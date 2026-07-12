from typing import Any
from typing import cast

from .query import Query
from .query_cache import MISSING
from .query_executor import QueryExecutor


class QueryExecutorMock(QueryExecutor):
    """Test double. Stub per-query return values with :meth:`stub_query`;
    un-stubbed queries fall through to the real :class:`QueryExecutor`.

    Build one with :meth:`QueryExecutor.create`, e.g.
    ``QueryExecutorMock.create(query_annotations=app.query_annotations)``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._query_stubs: dict[type, Any] = {}

    def stub_query[T](self, query_type: type[Query[T]], return_value: T) -> None:
        self._query_stubs[query_type] = return_value

    def fetch[T](self, query_obj: Query[T]) -> T:
        result = self._query_stubs.get(type(query_obj), MISSING)
        if result is not MISSING:
            return cast(T, result)
        return super().fetch(query_obj)

    async def afetch[T](self, query_obj: Query[T]) -> T:
        # Honour stubs on the async path too — the base ``afetch`` dispatches an
        # async handler to ``_afetch_async`` (bypassing ``fetch``), so the stub
        # check must live here rather than relying on the ``fetch`` override.
        result = self._query_stubs.get(type(query_obj), MISSING)
        if result is not MISSING:
            return cast(T, result)
        return await super().afetch(query_obj)

    def reset_mock(self) -> None:
        self._query_stubs.clear()
