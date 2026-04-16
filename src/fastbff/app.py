"""Top-level :class:`FastBFF` app — wires the queries registry, transformer registry,
DI container, and a per-process :class:`QueryExecutor` together.

Mirrors FastAPI's ``app = FastAPI(); app.include_router(router)`` ergonomics so
multi-module projects can register handlers locally and merge them into a
single composition root.
"""

from collections.abc import Callable
from typing import Any

from .exceptions import QueryRegistrationError
from .injections.registry import InjectorRegistry
from .query_executor.query_executor import QueryExecutor
from .query_executor.registry import QueriesRegistry
from .router import QueryRouter
from .transformer.registry import TransformerRegistry


class FastBFF:
    """Composition root for a fastbff application.

    Wires the four moving parts (DI container, queries registry, transformer
    registry, executor) so that user code only has to register handlers and
    call :meth:`render`::

        app = FastBFF()

        @app.queries
        def fetch_users(args: FetchUsers) -> dict[int, User]: ...

        @app.transformer
        def transform_owner(...): ...

        # ...or stage in a router and merge later:
        app.include_router(router)

        results = app.executor.render(TeamDTO, rows)

    All four collaborators are exposed as properties (``injector``,
    ``queries``, ``transformer``, ``executor``) so advanced callers can wire
    additional integrations (e.g., FastAPI ``app.dependency_overrides``).
    """

    def __init__(self) -> None:
        self._injector = InjectorRegistry()
        self._queries = QueriesRegistry(injector=self._injector)  # type: ignore[arg-type]
        self._transformer = TransformerRegistry(injector=self._injector)  # type: ignore[arg-type]
        self._executor = QueryExecutor(queries_registry=self._queries)
        self._injector.bind(QueryExecutor, lambda: self._executor)

    @property
    def injector(self) -> InjectorRegistry:
        return self._injector

    @property
    def queries(self) -> QueriesRegistry:
        """The app's :class:`QueriesRegistry` — usable as the ``@app.queries`` decorator."""
        return self._queries

    @property
    def transformer(self) -> TransformerRegistry:
        """The app's :class:`TransformerRegistry` — usable as the ``@app.transformer`` decorator."""
        return self._transformer

    @property
    def executor(self) -> QueryExecutor:
        """The app's process-wide :class:`QueryExecutor`.

        For request-scoped use under FastAPI, annotate handler parameters as
        ``Annotated[QueryExecutor, Depends(QueryExecutor)]`` so each HTTP
        request gets a fresh instance with a fresh cache.
        """
        return self._executor

    def bind(self, target: Any, factory: Callable[..., Any]) -> None:
        """Shortcut for :meth:`InjectorRegistry.bind` against the app's injector."""
        self._injector.bind(target, factory)

    def include_router(self, router: QueryRouter) -> None:
        """Merge a :class:`QueryRouter`'s registrations into this app.

        After include:

        * Every query registered on *router* is also looked up on the app's
          :class:`QueriesRegistry`, so ``app.executor.fetch(...)`` /
          ``app.executor.call(...)`` works against router-registered handlers.
        * The router's :class:`InjectorRegistry` is rewired to share the app's
          DI provider and context, so router-wrapped callables resolve their
          dependencies through the app's overrides at runtime — no need to
          rebuild any field info captured by ``build_transform_annotated``.

        Raises :class:`QueryRegistrationError` on duplicate registration of the
        same :class:`Query` subclass or function.
        """
        for query_type, annotation in router._queries._query_annotations.items():
            if query_type in self._queries._query_annotations:
                raise QueryRegistrationError(
                    f'Duplicate @queries registration for query type {query_type.__name__!r} '
                    f'when including router into FastBFF app.',
                )
            self._queries._query_annotations[query_type] = annotation
        for func, annotation in router._queries._func_annotations.items():
            if func in self._queries._func_annotations:
                raise QueryRegistrationError(
                    f'Duplicate @queries registration for function {func.__name__!r} '
                    f'when including router into FastBFF app.',
                )
            self._queries._func_annotations[func] = annotation

        # Rewire router's DI plumbing to the app's. Wrapped callables captured
        # by transformer field info reference the router's InjectorRegistry
        # through closure on `self`, so swapping the *attributes* on that
        # instance propagates the change to every already-wrapped callable.
        router._injector._dependency_provider = self._injector._dependency_provider
        router._injector._dependency_context = self._injector._dependency_context
