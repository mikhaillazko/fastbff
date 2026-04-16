"""Tests for ``QueryExecutorMock`` — stub/reset semantics."""

from dataclasses import dataclass
from unittest.mock import MagicMock

from fastbff.query_executor.mock import QueryExecutorMock
from fastbff.query_executor.query import Query


@dataclass(frozen=True)
class PlainResult:
    value: str


class FetchPlainQuery(Query[PlainResult]):
    key: str


def test_mock_stub_query_returns_stubbed_value(query_registry) -> None:
    # Arrange
    mock = QueryExecutorMock(queries_registry=query_registry)  # type: ignore[arg-type]
    expected = PlainResult(value='stubbed')
    mock.stub_query(FetchPlainQuery, expected)

    # Act
    result = mock.fetch(FetchPlainQuery(key='anything'))

    # Assert
    assert result is expected


def test_mock_reset_clears_query_stubs(query_registry) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: PlainResult(value=request.key))

    @query_registry
    def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return spy(request=query_args)

    mock = QueryExecutorMock(queries_registry=query_registry)  # type: ignore[arg-type]
    mock.stub_query(FetchPlainQuery, PlainResult(value='stubbed'))
    mock.reset_mock()

    # Act
    result = mock.fetch(FetchPlainQuery(key='real'))

    # Assert
    assert result.value == 'real'
    spy.assert_called_once()
