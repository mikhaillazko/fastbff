"""Top-level :class:`FastBFF` app — owns query registrations and plugs into a
user-owned FastAPI application via :meth:`FastBFF.mount`.

DI uses FastAPI's own ``solve_dependencies``: at finalize time fastbff
synthesizes a ``provide_query_executor`` factory whose signature declares the
union of every handler's and resolver's ``Annotated[..., Depends(...)]`` params
as keyword-only parameters. FastAPI resolves that graph once per request and
hands the resolved values to the :class:`QueryExecutor`. A second override
provides the :class:`SyncQueryExecutor` facade for sync endpoints.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated
from typing import Any
from typing import get_origin

from fastapi import Depends

from .di import build_provide_query_executor
from .di import build_provide_sync_query_executor
from .di import collect_dep_specs
from .exceptions import QueryNotRegisteredError
from .exceptions import QueryRegistrationError
from .exceptions import ResolveRegistrationError
from .query_executor.query import Query
from .query_executor.query_annotation import QueryAnnotation
from .query_executor.query_annotation import _is_query_subclass
from .query_executor.query_executor import QueryExecutor
from .query_executor.query_executor import SyncQueryExecutor
from .resolve import iter_resolves
from .router import QueryRouter


class FastBFF:
    """Composition root for a fastbff application.

    Wiring is two-phase:

    1. Register handlers with ``@app.queries`` (or merge a :class:`QueryRouter`
       via :meth:`include_router`).
    2. Call :meth:`finalize` (implicitly via :meth:`mount`) to synthesize the
       ``provide_query_executor`` factory from the union of all registered query
       and discovered resolver deps. Re-finalize is supported; the factory is
       rebuilt if new handlers were added.

    Async endpoints declare ``Annotated[QueryExecutor, Depends(QueryExecutor)]``
    and ``await query_executor.fetch(...)``; sync endpoints declare
    ``Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)]`` and
    ``sync_query_executor.fetch(...)``. :meth:`mount` registers the overrides
    for both in ``fastapi_app.dependency_overrides``.
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

    @property
    def query_annotations(self) -> Mapping[type, QueryAnnotation]:
        """The ``query_type → QueryAnnotation`` index built by ``@queries`` registrations.

        Returned as a read-only ``MappingProxyType`` view over the live
        registry — callers can iterate and look up entries, but cannot mutate
        the index. New ``@queries`` registrations show up automatically because
        the view is live, not a snapshot.

        Pass this to :class:`QueryExecutorMock` or a hand-built
        :class:`QueryExecutor` instead of reaching into ``app._query_annotations``.
        """
        return MappingProxyType(self._query_annotations)

    def queries[F: Callable](self, func_or_query_type: F | type[Query]) -> F | Callable[[F], F]:
        """Register *func* as a ``@queries`` handler.

        Supports both the plain decorator form and the decorator-factory form
        that binds an explicit :class:`Query` subclass for parameterless
        handlers::

            @app.queries
            def fetch_users(query: FetchUsers) -> dict[int, UserDTO]: ...

            @app.queries(FetchAllUsers)
            def fetch_all_users() -> list[UserDTO]: ...
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

    @property
    def router(self) -> QueryRouter:
        """The app's underlying :class:`QueryRouter` (query storage)."""
        return self._router

    def bind(self, target: Any, factory: Callable[..., Any]) -> None:
        """Add an override to ``self.dependency_overrides`` (FastAPI-compatible)."""
        key = target.__origin__ if get_origin(target) is Annotated else target
        self._overrides[key] = factory

    def get_annotation_by_query_type(self, query_type: type) -> QueryAnnotation:
        annotation = self._query_annotations.get(query_type)
        if annotation is not None:
            return annotation
        raise QueryNotRegisteredError(f'No @queries registered for query object {query_type}')

    def include_router(self, router: QueryRouter) -> None:
        """Merge *router*'s registrations into this app.

        Raises :class:`QueryRegistrationError` on duplicate registration of the
        same :class:`Query` subclass or query function, so copy-paste collisions
        cannot silently replace a previously registered handler.
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

        self._invalidate_finalize()

    def _discover_resolvers(self) -> list[Callable]:
        """Collect resolver functions referenced by ``Resolve(resolver=...)`` fields.

        Walks the response model of every query that needs rendering (recursing
        into nested models) so a resolver's ``Depends(...)`` params join the DI
        union without a second decorator.
        """
        resolvers: list[Callable] = []
        seen: set[Callable] = set()
        for annotation in self._query_annotations.values():
            target = annotation.render_target
            if target is None:
                continue
            _, model = target
            for resolve in iter_resolves(model):
                if resolve.resolver is not None and resolve.resolver not in seen:
                    seen.add(resolve.resolver)
                    resolvers.append(resolve.resolver)
        return resolvers

    def _invalidate_finalize(self) -> None:
        self._provide_query_executor = None
        self._finalized_for = None

    def _validate_resolve_targets(self) -> None:
        """Each ``Resolve(QueryType)`` must point at a registered :class:`EntityQuery`.

        Surfaces at composition time what would otherwise be a request-time
        error, matching the project rule that declaration mistakes blow up at
        registration.
        """
        for annotation in self._query_annotations.values():
            target = annotation.render_target
            if target is None:
                continue
            _, model = target
            for resolve in iter_resolves(model):
                query_type = resolve.query_type
                if query_type is None:
                    continue
                registered = self._query_annotations.get(query_type)
                if registered is None or not registered.is_entity:
                    name = getattr(query_type, '__name__', query_type)
                    raise ResolveRegistrationError(
                        f'Resolve({name}) targets a query that is not a registered EntityQuery. '
                        'Register it with @app.queries (or @router.queries) as an EntityQuery '
                        'before mounting.',
                    )

    def finalize(self) -> Callable:
        """Synthesize ``provide_query_executor`` from the current registrations.

        Idempotent — caches the result until a new handler is registered. Also
        installs the ``QueryExecutor`` and ``SyncQueryExecutor`` overrides in
        :attr:`dependency_overrides`.
        """
        query_handlers = list(self._router._query_func_annotations_registry.keys())
        resolvers = self._discover_resolvers()
        handlers = [*query_handlers, *resolvers]
        key = tuple(id(h) for h in handlers)
        if self._provide_query_executor is not None and self._finalized_for == key:
            return self._provide_query_executor

        self._validate_resolve_targets()

        specs, handler_index = collect_dep_specs(
            handlers,
            query_executor_type=QueryExecutor,
        )
        provide = build_provide_query_executor(
            specs=specs,
            handler_index=handler_index,
            query_annotations_factory=lambda: self._query_annotations,
            query_executor_factory=QueryExecutor.create,
        )
        self._provide_query_executor = provide
        self._finalized_for = key
        self._overrides[QueryExecutor] = provide
        self._overrides[SyncQueryExecutor] = build_provide_sync_query_executor(
            query_executor_type=QueryExecutor,
            sync_factory=SyncQueryExecutor.create,
        )
        return provide

    def mount(self, fastapi_app: Any) -> Callable:
        """Finalize and copy overrides into ``fastapi_app.dependency_overrides``.

        Returns the synthesized ``provide_query_executor`` callable so you can
        reference it directly on your endpoints if you don't want to rely on the
        ``QueryExecutor`` override.
        """
        provide = self.finalize()
        fastapi_app.dependency_overrides.update(self._overrides)
        return provide


# Re-export for typing ergonomics: ``Annotated[QueryExecutor, Depends(QueryExecutor)]``
# is the intended async-endpoint declaration; the app's override maps it to the
# synthesized factory at mount time.
_ = Depends
