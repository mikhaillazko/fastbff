"""Tests for ``QueryExecutorMock`` — stub/reset semantics."""

from dataclasses import dataclass
from unittest.mock import MagicMock

from fastbff.query_executor.query import Query
from fastbff.query_executor.query_executor_mock import QueryExecutorMock


@dataclass(frozen=True)
class PlainResult:
    value: str


class FetchPlainQuery(Query[PlainResult]):
    key: str


def test_mock_stub_query_returns_stubbed_value(app) -> None:
    # Arrange
    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    expected = PlainResult(value='stubbed')
    mock.stub_query(FetchPlainQuery, expected)

    # Act
    result = mock.fetch(FetchPlainQuery(key='anything'))

    # Assert
    assert result is expected


def test_mock_reset_clears_query_stubs(app) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return spy(request=query_args)

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    mock.stub_query(FetchPlainQuery, PlainResult(value='stubbed'))
    mock.reset_mock()

    # Act
    result = mock.fetch(FetchPlainQuery(key='real'))

    # Assert
    assert result.value == 'real'
    spy.assert_called_once()
