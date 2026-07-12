"""FastAPI-native DI integration for fastbff.

At finalize time, fastbff walks every registered query handler and every
discovered resolver, collects the union of their ``Annotated[..., Depends(...)]``
parameters, and synthesizes a ``provide_query_executor`` factory function whose
``__signature__`` declares those deps as keyword-only parameters. FastAPI's
``get_dependant`` reads ``__signature__`` and resolves the entire graph once
per request.

The resolved values are handed to a :class:`QueryExecutor` that stores them in
a per-handler lookup table. Handler / resolver dispatch becomes a dict lookup —
no second DI traversal. A ``QueryExecutor``-typed parameter (with or without
``Depends``) is bound to the executor itself at dispatch time rather than
resolved by FastAPI.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from inspect import Parameter
from inspect import Signature
from inspect import signature
from typing import Annotated
from typing import Any
from typing import get_args
from typing import get_origin

from fastapi import Depends
from fastapi.params import Depends as DependsParam

from .reflection import cached_type_hints

QUERY_EXECUTOR_SENTINEL = object()


@dataclass(frozen=True)
class DepSpec:
    """A single unique dependency pulled from the union of handler signatures."""

    synthetic_name: str
    annotation: Any
    depends: DependsParam


HandlerDepIndex = dict[Callable, dict[str, Any]]


def _iter_injectable_params(
    func: Callable,
    query_executor_type: type,
) -> Iterable[tuple[str, str, Any, DependsParam | None]]:
    """Yield ``(name, kind, annotation, depends)`` for injectable params of *func*.

    ``kind`` is ``'executor'`` for a parameter typed as the project's
    :class:`QueryExecutor` (bound to the executor at dispatch time, with or
    without an accompanying ``Depends``) or ``'depends'`` for an
    ``Annotated[T, Depends(...)]`` parameter. Every other parameter (the
    ``Query[T]`` object, a resolver's ``ids``) is skipped.
    """
    hints = cached_type_hints(func)
    for name, param in signature(func).parameters.items():
        annotation = hints.get(name, param.annotation)
        inner = annotation
        depends: DependsParam | None = None
        if get_origin(annotation) is Annotated:
            args = get_args(annotation)
            inner = args[0]
            for meta in args[1:]:
                if isinstance(meta, DependsParam):
                    depends = meta
                    break
        if inner is query_executor_type or (depends is not None and depends.dependency is query_executor_type):
            yield name, 'executor', annotation, depends
        elif depends is not None:
            yield name, 'depends', annotation, depends


def collect_dep_specs(
    handlers: Iterable[Callable],
    *,
    query_executor_type: type,
) -> tuple[list[DepSpec], HandlerDepIndex]:
    """Walk *handlers* and dedup their ``Depends`` params into a shared spec list.

    Params typed as the project's :class:`QueryExecutor` are excluded from the
    synthesized signature (they're bound to the executor at dispatch time) —
    including them would produce a self-referential dep graph because
    ``QueryExecutor`` itself is bound to ``provide_query_executor`` via
    ``app.dependency_overrides``.

    Returns:
        (specs, handler_index) where ``specs`` is the deduped list of unique
        dependencies and ``handler_index[func][arg_name]`` maps each handler
        param to either the synthetic name (string) or the
        :data:`QUERY_EXECUTOR_SENTINEL`.
    """
    specs: list[DepSpec] = []
    dedup: dict[tuple[Any, bool], str] = {}
    handler_index: HandlerDepIndex = {}

    for handler in handlers:
        per_handler: dict[str, Any] = {}
        for arg_name, kind, annotation, depends in _iter_injectable_params(handler, query_executor_type):
            if kind == 'executor':
                per_handler[arg_name] = QUERY_EXECUTOR_SENTINEL
                continue
            assert depends is not None
            key = (depends.dependency, depends.use_cache)
            synthetic = dedup.get(key)
            if synthetic is None:
                synthetic = f'__dep_{len(specs)}'
                specs.append(
                    DepSpec(
                        synthetic_name=synthetic,
                        annotation=annotation,
                        depends=depends,
                    ),
                )
                dedup[key] = synthetic
            per_handler[arg_name] = synthetic
        if per_handler:
            handler_index[handler] = per_handler

    return specs, handler_index


def build_provide_query_executor(
    *,
    specs: list[DepSpec],
    handler_index: HandlerDepIndex,
    query_annotations_factory: Callable[[], dict[type, Any]],
    query_executor_factory: Callable[..., Any],
) -> Callable:
    """Build a ``provide_query_executor(**deps)`` factory with a synthesized signature.

    The returned function is suitable for ``Depends(provide_query_executor)``
    on FastAPI endpoints — FastAPI will resolve every entry in *specs* and
    pass them as kwargs. *query_executor_factory* is called with the resolved
    map to construct a :class:`QueryExecutor` (in practice
    ``QueryExecutor.create``).
    """

    def provide_query_executor(**resolved: Any) -> Any:
        return query_executor_factory(
            query_annotations=query_annotations_factory(),
            resolved_deps=resolved,
            handler_index=handler_index,
        )

    parameters = [
        Parameter(
            name=spec.synthetic_name,
            kind=Parameter.KEYWORD_ONLY,
            annotation=Annotated[spec.annotation, Depends(spec.depends.dependency, use_cache=spec.depends.use_cache)]
            if get_origin(spec.annotation) is not Annotated
            else spec.annotation,
        )
        for spec in specs
    ]
    provide_query_executor.__signature__ = Signature(parameters=parameters)  # type: ignore[attr-defined]
    provide_query_executor.__annotations__ = {spec.synthetic_name: spec.annotation for spec in specs}
    provide_query_executor.__name__ = 'provide_query_executor'
    return provide_query_executor


def build_provide_sync_query_executor(
    *,
    query_executor_type: type,
    sync_factory: Callable[[Any], Any],
) -> Callable:
    """Build a ``provide_sync_query_executor`` that wraps the resolved executor.

    Depends on the async :class:`QueryExecutor` (resolved through the same
    mount-time override) and wraps it in a :class:`SyncQueryExecutor` via
    *sync_factory* (in practice ``SyncQueryExecutor.create``).
    """
    inner_annotation = Annotated[query_executor_type, Depends(query_executor_type)]

    def provide_sync_query_executor(inner: Any) -> Any:
        return sync_factory(inner)

    provide_sync_query_executor.__signature__ = Signature(  # type: ignore[attr-defined]
        parameters=[Parameter(name='inner', kind=Parameter.KEYWORD_ONLY, annotation=inner_annotation)],
    )
    provide_sync_query_executor.__annotations__ = {'inner': inner_annotation}
    provide_sync_query_executor.__name__ = 'provide_sync_query_executor'
    return provide_sync_query_executor
