"""Top-level :class:`FastBFF` app — owns query / transformer registrations and
plugs into a user-owned FastAPI application via :meth:`FastBFF.mount`.

DI uses FastAPI's own ``solve_dependencies``: at finalize time fastbff
synthesizes a ``provide_query_executor`` factory whose signature declares
the union of every handler's ``Annotated[..., Depends(...)]`` params as
keyword-only parameters. FastAPI resolves that graph once per request and
hands the resolved values to the :class:`QueryExecutor`.

Offline callers (scripts, tests) use :meth:`FastBFF.entrypoint`, which drives
FastAPI's ``solve_dependencies`` through a synthetic ``Request`` via
``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from functools import wraps
from typing import Annotated
from typing import Any
from typing import get_origin

from fastapi import Depends
from fastapi import Request
from fastapi.dependencies.utils import get_dependant
from fastapi.dependencies.utils import solve_dependencies

from .di import build_provide_query_executor
from .di import collect_dep_specs
from .exceptions import QueryNotRegisteredError
from .exceptions import QueryRegistrationError
from .query_executor.query import Query
from .query_executor.query_annotation import QueryAnnotation
from .query_executor.query_annotation import _is_query_subclass
from .query_executor.query_executor import QueryExecutor
from .router import QueryRouter


class FastBFF:
    """Composition root for a fastbff application.

    Wiring is two-phase:

    1. Register handlers with ``@app.queries`` / ``@app.transformer`` (or
       merge a :class:`QueryRouter` via :meth:`include_router`).
    2. Call :meth:`finalize` (implicitly via :meth:`mount` or
       :meth:`entrypoint`) to synthesize the ``provide_query_executor``
       factory from the union of all registered deps. Re-finalize is
       supported; the factory is rebuilt if new handlers were added.

    Endpoints declare ``Annotated[QueryExecutor, Depends(QueryExecutor)]``.
    :meth:`mount` registers an override that points ``QueryExecutor`` at the
    synthesized factory in ``fastapi_app.dependency_overrides``.
    """

    def __init__(self) -> None:
        self._router = QueryRouter()
        self._query_annotations: dict[type, QueryAnnotation] = {}
        self._overrides: dict[Callable, Callable] = {}
        self._provide_query_executor: Callable | None = None
        self._finalized_for: tuple[int, ...] | None = None

    @property
    def dependency_overrides(self) -> dict[Callable, Callable]:
        """Compatible with FastAPI's ``dependency_overrides_provider`` protocol."""
        return self._overrides

    def queries[F: Callable](self, func_or_query_type: F | type[Query]) -> F | Callable[[F], F]:
        """Register *func* as a ``@query`` handler.

        Supports both the plain decorator form and the decorator-factory form
        that binds an explicit :class:`Query` subclass for parameterless
        handlers::

            @app.queries
            def fetch_users(args: FetchUsers) -> dict[int, User]: ...

            @app.queries(FetchAllUsers)
            def fetch_all_users() -> list[User]: ...
        """
        if _is_query_subclass(func_or_query_type):

            def decorator(func: F) -> F:
                return self._register_query(func, explicit_query_type=func_or_query_type)

            return decorator
        return self._register_query(func_or_query_type)

    def _register_query[F: Callable](self, func: F, explicit_query_type: type[Query] | None = None) -> F:
        self._router._register(func, explicit_query_type=explicit_query_type)
        annotation = self._router._query_func_annotations_registry[func]
        if annotation.query_type is not None:
            if annotation.query_type in self._query_annotations:
                raise QueryRegistrationError(
                    f'Duplicate @queries registration for query type {annotation.query_type.__name__!r}.',
                )
            self._query_annotations[annotation.query_type] = annotation
        self._invalidate_finalize()
        return func

    def transformer[F: Callable](self, func: F) -> F:
        """Register *func* as a ``@transformer``."""
        self._router.transformer(func)
        self._invalidate_finalize()
        return func

    @property
    def router(self) -> QueryRouter:
        """The app's underlying :class:`QueryRouter` (query + transformer storage)."""
        return self._router

    def bind(self, target: Any, factory: Callable[..., Any]) -> None:
        """Add an override to ``self.dependency_overrides`` (FastAPI-compatible)."""
        key = target.__origin__ if get_origin(target) is Annotated else target
        self._overrides[key] = factory

    def get_annotation_by_query_type(self, query_type: type) -> QueryAnnotation:
        annotation = self._query_annotations.get(query_type)
        if annotation is not None:
            return annotation
        raise QueryNotRegisteredError(f'No @query registered for query object {query_type}')

    def include_router(self, router: QueryRouter) -> None:
        """Merge *router*'s registrations into this app.

        Raises :class:`QueryRegistrationError` on duplicate registration of
        the same :class:`Query` subclass or function.
        """
        for func, annotation in router._query_func_annotations_registry.items():
            if func in self._router._query_func_annotations_registry:
                raise QueryRegistrationError(
                    f'Duplicate @queries registration for function {func.__name__!r} '
                    f'when including router into FastBFF app.',
                )
            if annotation.query_type is not None:
                if annotation.query_type in self._query_annotations:
                    raise QueryRegistrationError(
                        f'Duplicate @queries registration for query type {annotation.query_type.__name__!r} '
                        f'when including router into FastBFF app.',
                    )
                self._query_annotations[annotation.query_type] = annotation
            self._router._query_func_annotations_registry[func] = annotation

        for func, transformer_annotation in router._transformer_func_annotation_registry.items():
            self._router._transformer_func_annotation_registry[func] = transformer_annotation

        self._invalidate_finalize()

    def _all_handlers(self) -> list[Callable]:
        return [
            *self._router._query_func_annotations_registry.keys(),
            *self._router._transformer_func_annotation_registry.keys(),
        ]

    def _invalidate_finalize(self) -> None:
        self._provide_query_executor = None
        self._finalized_for = None

    def finalize(self) -> Callable:
        """Synthesize ``provide_query_executor`` from the current registrations.

        Idempotent — caches the result until a new handler is registered.
        Also installs ``QueryExecutor → provide_query_executor`` in
        :attr:`dependency_overrides` so endpoints can use
        ``Annotated[QueryExecutor, Depends(QueryExecutor)]``.
        """
        handlers = self._all_handlers()
        key = tuple(id(h) for h in handlers)
        if self._provide_query_executor is not None and self._finalized_for == key:
            return self._provide_query_executor

        specs, handler_index = collect_dep_specs(
            handlers,
            query_executor_type=QueryExecutor,
        )
        provide = build_provide_query_executor(
            specs=specs,
            handler_index=handler_index,
            query_annotations_factory=lambda: self._query_annotations,
            query_executor_cls=QueryExecutor,
        )
        self._provide_query_executor = provide
        self._finalized_for = key
        self._overrides[QueryExecutor] = provide
        return provide

    def mount(self, fastapi_app: Any) -> Callable:
        """Finalize and copy overrides into ``fastapi_app.dependency_overrides``.

        Returns the synthesized ``provide_query_executor`` callable so you
        can reference it directly on your endpoints if you don't want to
        rely on the ``QueryExecutor`` override.
        """
        provide = self.finalize()
        fastapi_app.dependency_overrides.update(self._overrides)
        return provide

    def entrypoint[F: Callable](self, func: F) -> F:
        """Wrap *func* as an offline entrypoint that resolves its ``Depends``.

        Drives FastAPI's ``solve_dependencies`` via ``asyncio.run`` against
        a synthetic ``Request``, applying the app's overrides (including the
        ``QueryExecutor → provide_query_executor`` binding). Use for CLIs,
        scripts, and tests that want ``@app.entrypoint`` ergonomics without
        spinning up an HTTP server.
        """

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.finalize()
            return asyncio.run(self._run_entrypoint(func, args, kwargs))

        return wrapper  # type: ignore[return-value]

    async def _run_entrypoint(
        self,
        func: Callable,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        dependant = get_dependant(path='/__entrypoint__', call=func)
        async with AsyncExitStack() as stack, AsyncExitStack() as function_stack:
            request = Request(
                scope={
                    'type': 'http',
                    'method': 'GET',
                    'path': '/__entrypoint__',
                    'raw_path': b'/__entrypoint__',
                    'query_string': b'',
                    'headers': [],
                    'fastapi_inner_astack': stack,
                    'fastapi_function_astack': function_stack,
                },
            )
            solved = await solve_dependencies(
                request=request,
                dependant=dependant,
                async_exit_stack=stack,
                embed_body_fields=False,
                dependency_overrides_provider=self,
            )
            resolved_values = _extract_solved_values(solved)
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs, **resolved_values)
            return func(*args, **kwargs, **resolved_values)


def _extract_solved_values(solved: Any) -> dict[str, Any]:
    """Pull the resolved kwargs out of FastAPI's ``SolvedDependency`` / tuple.

    FastAPI 0.112+ returns a ``SolvedDependency`` namedtuple/dataclass whose
    first field is ``values``; older versions return a plain tuple whose
    first element is the values dict.
    """
    values = getattr(solved, 'values', None)
    if values is not None:
        return values
    return solved[0]


# Re-export for typing ergonomics: ``Annotated[QueryExecutor, Depends(QueryExecutor)]``
# is the intended endpoint declaration; the app's override maps it to the
# synthesized factory at mount time.
_ = Depends
