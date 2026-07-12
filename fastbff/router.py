"""Carrier for pending query registrations.

A :class:`QueryRouter` gathers query handlers with their type metadata. When
the router is merged into a :class:`FastBFF` app via
:meth:`FastBFF.include_router`, its registrations join the union of handlers
scanned by :meth:`FastBFF.finalize` to synthesize the ``provide_query_executor``
factory.

Resolvers are not registered here — they are discovered from the ``Resolve``
fields of the response models a query returns, so a resolver's ``Depends(...)``
params are collected without a second decorator.
"""

from collections.abc import Callable

from .exceptions import QueryRegistrationError
from .query_executor.query import Query
from .query_executor.query_annotation import QueryAnnotation
from .query_executor.query_annotation import _is_query_subclass


class QueryRouter:
    """A bundle of query registrations not yet attached to an app.

    Use ``@router.queries`` exactly like the decorator on a :class:`FastBFF`
    app. Pass the router to :meth:`FastBFF.include_router` to merge its
    registrations into the app::

        router = QueryRouter()

        @router.queries
        def fetch_users(query: FetchUsers, session: DBSession) -> dict[int, UserDTO]: ...

        app = FastBFF()
        app.include_router(router)
    """

    def __init__(self) -> None:
        self._query_func_annotations_registry: dict[Callable, QueryAnnotation] = {}

    def queries[F: Callable](self, func_or_query_type: F | type[Query]) -> F | Callable[[F], F]:
        """Register *func* as a ``@queries`` handler on this router.

        Supports two forms::

            @router.queries
            def fetch_users(query: FetchUsers) -> dict[int, UserDTO]: ...

            @router.queries(FetchAllUsers)
            def fetch_all_users() -> list[UserDTO]: ...

        The second form binds *func* to an explicit ``Query`` subclass for
        parameterless handlers whose query type cannot be inferred from the
        signature.
        """
        if _is_query_subclass(func_or_query_type):
            return self._make_decorator(explicit_query_type=func_or_query_type)
        return self._register(func=func_or_query_type)

    def _make_decorator[F: Callable](self, explicit_query_type: type[Query]) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            return self._register(func=func, explicit_query_type=explicit_query_type)

        return decorator

    def _register[F: Callable](self, func: F, explicit_query_type: type[Query] | None = None) -> F:
        if func in self._query_func_annotations_registry:
            raise QueryRegistrationError(
                f'Duplicate @queries registration for function {func.__name__!r}.',
            )
        annotation = QueryAnnotation(original_func=func, explicit_query_type=explicit_query_type)
        self._query_func_annotations_registry[func] = annotation
        return func
