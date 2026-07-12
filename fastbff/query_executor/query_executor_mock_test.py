"""Tests for ``QueryExecutorMock`` — stub/reset semantics."""

import asyncio
from dataclasses import dataclass
from unittest.mock import MagicMock

from fastbff.query_executor.query import Query
from fastbff.query_executor.query_executor_mock import QueryExecutorMock


@dataclass(frozen=True)
class _PlainResult:
    value: str


class _FetchPlainQuery(Query[_PlainResult]):
    key: str


def test_mock_stub_query_returns_stubbed_value(app) -> None:
    # Arrange
    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    expected = _PlainResult(value='stubbed')
    mock.stub_query(_FetchPlainQuery, expected)

    # Act
    result = mock.fetch(_FetchPlainQuery(key='anything'))

    # Assert
    assert result is expected


def test_mock_afetch_honours_stub_over_async_handler(app) -> None:
    """A stubbed query short-circuits ``afetch`` even when the real handler is
    async — the async path dispatches to ``_afetch_async`` (bypassing ``fetch``),
    so the mock must guard ``afetch`` too."""
    calls = 0

    @app.queries
    async def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        nonlocal calls
        calls += 1
        return _PlainResult(value=query_args.key)

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    stub = _PlainResult(value='stubbed')
    mock.stub_query(_FetchPlainQuery, stub)

    # Act
    result = asyncio.run(mock.afetch(_FetchPlainQuery(key='real')))

    # Assert
    assert result is stub
    assert calls == 0


def test_mock_reset_clears_query_stubs(app) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: _PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return spy(request=query_args)

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    mock.stub_query(_FetchPlainQuery, _PlainResult(value='stubbed'))
    mock.reset_mock()

    # Act
    result = mock.fetch(_FetchPlainQuery(key='real'))

    # Assert
    assert result.value == 'real'
    spy.assert_called_once()
