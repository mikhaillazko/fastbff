"""FastAPI-native DI integration for fastbff.

At finalize time, fastbff walks every registered query/transformer handler,
collects the union of their ``Annotated[..., Depends(...)]`` parameters, and
synthesizes a ``provide_query_executor`` factory function whose
``__signature__`` declares those deps as keyword-only parameters. FastAPI's
``get_dependant`` reads ``__signature__`` and resolves the entire graph once
per request.

The resolved values are handed to a :class:`QueryExecutor` that stores them in
a per-handler lookup table. Handler / transformer dispatch becomes a dict
lookup — no second DI traversal.
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


def _iter_depends_params(func: Callable) -> Iterable[tuple[str, Any, DependsParam]]:
    hints = cached_type_hints(func)
    for name, param in signature(func).parameters.items():
        annotation = hints.get(name, param.annotation)
        if get_origin(annotation) is not Annotated:
            continue
        args = get_args(annotation)
        for meta in args[1:]:
            if isinstance(meta, DependsParam):
                yield name, annotation, meta
                break


def _is_query_executor_dep(depends: DependsParam, annotation: Any, query_executor_type: type) -> bool:
    dep = depends.dependency
    if dep is query_executor_type:
        return True
    inner = get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
    return inner is query_executor_type


def collect_dep_specs(
    handlers: Iterable[Callable],
    *,
    query_executor_type: type,
) -> tuple[list[DepSpec], HandlerDepIndex]:
    """Walk *handlers* and dedup their ``Depends`` params into a shared spec list.

    Params typed as the project's :class:`QueryExecutor` are excluded from the
    synthesized signature (they're resolved from validation context at dispatch
    time) — including them would produce a self-referential dep graph because
    ``QueryExecutor`` itself is bound to ``provide_query_executor`` via
    ``app.dependency_overrides``.

    Returns:
        (specs, handler_index) where ``specs`` is the deduped list of
        unique dependencies and ``handler_index[func][arg_name]`` maps each
        handler param to either the synthetic name (string) or the
        :data:`QUERY_EXECUTOR_SENTINEL`.
    """
    specs: list[DepSpec] = []
    dedup: dict[tuple[Any, bool], str] = {}
    handler_index: HandlerDepIndex = {}

    for handler in handlers:
        per_handler: dict[str, Any] = {}
        for arg_name, annotation, depends in _iter_depends_params(handler):
            if _is_query_executor_dep(depends, annotation, query_executor_type):
                per_handler[arg_name] = QUERY_EXECUTOR_SENTINEL
                continue
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
