"""Static discovery of ``QueryExecutor.fetch(...)`` calls inside a transformer body.

Best-effort AST walk that returns the set of :class:`Query` subclasses a
transformer dispatches into. Used by :meth:`FastBFF.finalize` to surface
"transformer fetches an unregistered query" at composition time instead of
the first time a request hits the transformer.

Best-effort means: misses are silent (no false positives). If the discovery
cannot prove what a fetch dispatches into — e.g. ``self.qe.fetch(...)``,
or a fetch through a callable returned by another function — the call is
skipped. A query the validator could not see is *not* reported as missing.

Recognised idioms:

* ``query_executor.fetch(FetchUsers(...))`` — direct call.
* Aliased executor parameter names — the parameter is identified by its
  ``Annotated[QueryExecutor, Depends(...)]`` annotation, not its name.
* ``q = FetchUsers(...); query_executor.fetch(q)`` — single-assignment
  Name binding inside the function body is propagated.
* Query class resolved via the function's closure cells in addition to
  ``__globals__``.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable

from fastbff.di import _is_query_executor_dep
from fastbff.di import _iter_depends_params


def discover_fetched_queries(transformer_func: Callable, *, query_executor_type: type) -> set[type]:
    """Return the set of ``Query`` subclasses *transformer_func* calls ``fetch()`` on.

    Returns an empty set when the source cannot be retrieved (e.g. the
    function was defined in the REPL, or ``inspect.getsource`` fails).
    """
    try:
        source = textwrap.dedent(inspect.getsource(transformer_func))
    except (OSError, TypeError):
        return set()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    executor_param_names = _executor_param_names(transformer_func, query_executor_type)
    if not executor_param_names:
        return set()

    name_resolver = _NameResolver(transformer_func)

    fetched: set[type] = set()
    for func_node in _iter_function_defs(tree):
        local_query_bindings = _collect_query_call_bindings(func_node, name_resolver)
        for call_node in ast.walk(func_node):
            if not isinstance(call_node, ast.Call):
                continue
            if not _is_executor_fetch_call(call_node, executor_param_names):
                continue
            if not call_node.args:
                continue
            query_cls = _resolve_query_class(
                call_node.args[0],
                name_resolver=name_resolver,
                local_bindings=local_query_bindings,
            )
            if query_cls is not None:
                fetched.add(query_cls)
    return fetched


def _executor_param_names(transformer_func: Callable, query_executor_type: type) -> set[str]:
    return {
        arg_name
        for arg_name, annotation, depends in _iter_depends_params(transformer_func)
        if _is_query_executor_dep(depends, annotation, query_executor_type)
    }


def _iter_function_defs(tree: ast.AST) -> list[ast.AST]:
    """Return the outermost function definition(s) in *tree*.

    ``inspect.getsource`` on a free function returns a single ``FunctionDef``;
    on a method, it can return a class body. We walk the top-level so both
    shapes are covered, and we deliberately do not descend into nested
    function bodies — a closure created inside the transformer is its own
    scope and could rebind ``query_executor``.
    """
    out: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
            return out
    return out


def _is_executor_fetch_call(call_node: ast.Call, executor_param_names: set[str]) -> bool:
    func = call_node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == 'fetch'
        and isinstance(func.value, ast.Name)
        and func.value.id in executor_param_names
    )


def _collect_query_call_bindings(
    func_node: ast.AST,
    name_resolver: _NameResolver,
) -> dict[str, type]:
    """Map local-name → resolved Query class for assignments of the form ``q = FetchUsers(...)``.

    Only single-target ``Name = Call(Name(...))`` assignments are tracked. If
    the same name is assigned more than once, drop it — we cannot prove
    which binding was live at the ``fetch`` call site without a real CFG.
    """
    bindings: dict[str, type] = {}
    seen: set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id in seen:
            bindings.pop(target.id, None)
            continue
        seen.add(target.id)
        value = node.value
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)):
            continue
        cls = name_resolver.resolve(value.func.id)
        if cls is not None:
            bindings[target.id] = cls
    return bindings


def _resolve_query_class(
    arg_node: ast.expr,
    *,
    name_resolver: _NameResolver,
    local_bindings: dict[str, type],
) -> type | None:
    if isinstance(arg_node, ast.Call) and isinstance(arg_node.func, ast.Name):
        return name_resolver.resolve(arg_node.func.id)
    if isinstance(arg_node, ast.Name):
        return local_bindings.get(arg_node.id)
    return None


class _NameResolver:
    """Resolve a bare name against the function's globals and closure cells."""

    def __init__(self, func: Callable) -> None:
        self._globals = getattr(func, '__globals__', {})
        closure = getattr(func, '__closure__', None) or ()
        freevars = getattr(getattr(func, '__code__', None), 'co_freevars', ()) or ()
        self._closure: dict[str, object] = {}
        for name, cell in zip(freevars, closure, strict=False):
            try:
                self._closure[name] = cell.cell_contents
            except ValueError:
                continue

    def resolve(self, name: str) -> type | None:
        value = self._closure.get(name)
        if value is None:
            value = self._globals.get(name)
        if isinstance(value, type):
            return value
        return None
