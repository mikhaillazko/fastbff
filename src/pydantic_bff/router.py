"""Local registries for staged registration prior to attaching to a :class:`FastBFF` app.

Mirrors FastAPI's :class:`fastapi.APIRouter` ergonomics: register your
``@router.queries`` and ``@router.transformer`` decorators against a router,
then attach the whole bundle to an app via :meth:`FastBFF.include_router`.
"""

from .injections.registry import InjectorRegistry
from .query_executor.registry import QueriesRegistry
from .transformer.registry import TransformerRegistry


class QueryRouter:
    """A bundle of query and transformer registrations not yet attached to an app.

    Use ``@router.queries`` and ``@router.transformer`` exactly like the
    decorators on a :class:`FastBFF` app. Pass the router to
    :meth:`FastBFF.include_router` to merge its registrations into the app::

        router = QueryRouter()

        @router.queries
        def fetch_users(args: FetchUsers) -> dict[int, User]: ...

        @router.transformer(prefetch=FetchUsers)
        def transform_owner(owner_id: int, batch: BatchArg[int],
                            query_executor: QueryExecutor) -> User | None: ...

        app = FastBFF()
        app.include_router(router)

    Registrations capture the router's own :class:`InjectorRegistry`. When the
    router is included in an app, the router's DI plumbing is swapped to share
    the app's, so already-wrapped callables resolve dependencies through the
    app's overrides at runtime.
    """

    def __init__(self) -> None:
        self._injector = InjectorRegistry()
        self._queries = QueriesRegistry(injector=self._injector)  # type: ignore[arg-type]
        self._transformer = TransformerRegistry(injector=self._injector)  # type: ignore[arg-type]

    @property
    def queries(self) -> QueriesRegistry:
        """The router's :class:`QueriesRegistry` — usable as the ``@router.queries`` decorator."""
        return self._queries

    @property
    def transformer(self) -> TransformerRegistry:
        """The router's :class:`TransformerRegistry` — usable as the ``@router.transformer`` decorator."""
        return self._transformer

    @property
    def injector(self) -> InjectorRegistry:
        """The router's local :class:`InjectorRegistry`."""
        return self._injector
