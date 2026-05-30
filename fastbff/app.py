"""Top-level :class:`FastBFF` app — owns query / transformer registrations and
plugs into a user-owned FastAPI application via :meth:`FastBFF.mount`.

DI uses FastAPI's own ``solve_dependencies``: at finalize time fastbff
synthesizes a ``provide_query_executor`` factory whose signature declares
the union of every handler's ``Annotated[..., Depends(...)]`` params as
keyword-only parameters. FastAPI resolves that graph once per request and
hands the resolved values to the :class:`QueryExecutor`.
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
from .di import collect_dep_specs
from .exceptions import QueryNotRegisteredError
from .exceptions import QueryRegistrationError
from .exceptions import TransformerRegistrationError
from .query_executor.query import Query
from .query_executor.query_annotation import QueryAnnotation
from .query_executor.query_annotation import _is_query_subclass
from .query_executor.query_executor import QueryExecutor
from .router import QueryRouter
from .transformer.fetch_discovery import discover_fetched_queries


class FastBFF:
    """Composition root for a fastbff application.

    Wiring is two-phase:

    1. Register handlers with ``@app.queries`` / ``@app.transformer`` (or
       merge a :class:`QueryRouter` via :meth:`include_router`).
    2. Call :meth:`finalize` (implicitly via :meth:`mount`) to synthesize
       the ``provide_query_executor`` factory from the union of all
       registered deps. Re-finalize is supported; the factory is rebuilt
       if new handlers were added.

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

    @property
    def query_annotations(self) -> Mapping[type, QueryAnnotation]:
        """The ``query_type → QueryAnnotation`` index built by ``@queries`` registrations.

        Returned as a read-only ``MappingProxyType`` view over the live
        registry — callers can iterate and look up entries, but cannot
        mutate the index out from under the app. New ``@queries``
        registrations show up automatically because the view is live, not
        a snapshot.

        Pass this to :class:`QueryExecutorMock` or a hand-built
        :class:`QueryExecutor` instead of reaching into
        ``app._query_annotations``.
        """
        return MappingProxyType(self._query_annotations)

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
        the same :class:`Query` subclass or query function, and
        :class:`TransformerRegistrationError` on duplicate registration of
        the same transformer function. Both checks fire at composition
        time so copy-paste collisions cannot silently replace a previously
        registered handler.
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
            if func in self._router._transformer_func_annotation_registry:
                raise TransformerRegistrationError(
                    f'Duplicate @transformer registration for function {func.__name__!r} '
                    f'when including router into FastBFF app.',
                )
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

    def _validate_transformer_fetch_targets(self) -> None:
        """Best-effort: each transformer's fetch targets must be registered.

        Static AST walk over each transformer body to collect
        ``query_executor.fetch(<QueryCls>(...))`` calls (see
        :func:`discover_fetched_queries` for recognised idioms). Any fetched
        :class:`Query` subclass that isn't in the registry raises
        :class:`TransformerRegistrationError` here, surfacing what would
        otherwise be a request-time :class:`QueryNotRegisteredError`.

        Misses are silent — discovery cannot resolve every shape (e.g.
        ``self.qe.fetch(...)``), so we never *invent* a missing target.
        """
        registered = self._query_annotations
        for transformer_func in self._router._transformer_func_annotation_registry:
            fetched = discover_fetched_queries(transformer_func, query_executor_type=QueryExecutor)
            missing = [
                cls for cls in fetched if isinstance(cls, type) and issubclass(cls, Query) and cls not in registered
            ]
            if missing:
                names = ', '.join(sorted(cls.__name__ for cls in missing))
                raise TransformerRegistrationError(
                    f'Transformer {transformer_func.__name__!r} fetches query types that are '
                    f'not registered: {names}. Register them with @app.queries (or '
                    f'@router.queries) before mounting.',
                )

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

        self._validate_transformer_fetch_targets()

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


# Re-export for typing ergonomics: ``Annotated[QueryExecutor, Depends(QueryExecutor)]``
# is the intended endpoint declaration; the app's override maps it to the
# synthesized factory at mount time.
_ = Depends
