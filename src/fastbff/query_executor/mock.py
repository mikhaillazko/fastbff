from typing import Any
from typing import cast

from .query import Query
from .query_cache import MISSING
from .query_executor import QueryExecutor
from .registry import IQueriesRegistry


class QueryExecutorMock(QueryExecutor):
    """Test double. Stub per-query return values with :meth:`stub_query`;
    un-stubbed queries fall through to the real :class:`QueryExecutor`.
    """

    def __init__(self, queries_registry: IQueriesRegistry) -> None:
        super().__init__(queries_registry)
        self._query_stubs: dict[type, Any] = {}

    def stub_query[T](self, query_type: type[Query[T]], return_value: T) -> None:
        self._query_stubs[query_type] = return_value

    def fetch[T](self, query_obj: Query[T]) -> T:
        result = self._query_stubs.get(type(query_obj), MISSING)
        if result is not MISSING:
            return cast(T, result)
        return super().fetch(query_obj)

    def reset_mock(self) -> None:
        self._query_stubs.clear()
