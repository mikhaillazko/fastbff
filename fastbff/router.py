"""Carrier for pending query / transformer registrations.

A :class:`QueryRouter` gathers functions with their type metadata. When the
router is merged into a :class:`FastBFF` app via :meth:`FastBFF.include_router`,
its registrations become part of the union of handlers scanned by
:meth:`FastBFF.finalize` to synthesize the ``provide_query_executor`` factory.
"""

from collections.abc import Callable

from .query_executor.query import Query
from .query_executor.query_annotation import QueryAnnotation
from .query_executor.query_annotation import _is_query_subclass
from .transformer.types import _TRANSFORMER_ANNOTATION_ATTR
from .transformer.types import TransformerAnnotation


class QueryRouter:
    """A bundle of query and transformer registrations not yet attached to an app.

    Use ``@router.queries`` and ``@router.transformer`` exactly like the
    decorators on a :class:`FastBFF` app. Pass the router to
    :meth:`FastBFF.include_router` to merge its registrations into the app::

        router = QueryRouter()

        @router.queries
        def fetch_users(args: FetchUsers) -> dict[int, User]: ...

        @router.transformer
        def transform_owner(
            owner_id: int,
            batch: BatchArg[int],
            query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
        ) -> User | None: ...

        app = FastBFF()
        app.include_router(router)

    """

    def __init__(self) -> None:
        self._query_func_annotations_registry: dict[Callable, QueryAnnotation] = {}
        self._transformer_func_annotation_registry: dict[Callable, TransformerAnnotation] = {}

    def queries[F: Callable](self, func_or_query_type: F | type[Query]) -> F | Callable[[F], F]:
        """Register *func* as a ``@query`` handler on this router.

        Supports two forms::

            @router.queries
            def fetch_users(args: FetchUsers) -> dict[int, User]: ...

            @router.queries(FetchAllUsers)
            def fetch_all_users() -> list[User]: ...

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
        annotation = QueryAnnotation(original_func=func, explicit_query_type=explicit_query_type)
        self._query_func_annotations_registry[func] = annotation
        return func

    def transformer[F: Callable](self, func: F) -> F:
        """Register *func* as a ``@transformer`` handler on this router."""
        transformer_annotation = TransformerAnnotation(original_func=func)
        setattr(func, _TRANSFORMER_ANNOTATION_ATTR, transformer_annotation)
        self._transformer_func_annotation_registry[func] = transformer_annotation
        return func
