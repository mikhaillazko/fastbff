"""Tests for ``QueryExecutorMock`` — stub/reset semantics.

``fetch`` is a coroutine, so each scenario drives it with ``asyncio.run`` — no
pytest-asyncio plugin required. Stubs short-circuit the real handler on both
direct ``await mock.fetch(...)`` calls and the render pipeline (``Resolve``).
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel

from fastbff import EntityQuery
from fastbff import Query
from fastbff import QueryExecutorMock
from fastbff import Resolve


@dataclass(frozen=True)
class _PlainResult:
    value: str


class _UserDTO(BaseModel):
    id: int
    name: str = ''


class _FetchPlainQuery(Query[_PlainResult]):
    key: str


class _FetchUsers(EntityQuery[int, _UserDTO]):
    ids: frozenset[int]


def test_mock_stub_query_returns_stub_without_calling_handler(app) -> None:
    """A stubbed query short-circuits ``fetch`` — the (async) handler never runs."""
    calls = 0

    @app.queries
    async def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        nonlocal calls
        calls += 1
        return _PlainResult(value=query.key)

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    stub = _PlainResult(value='stubbed')
    mock.stub_query(_FetchPlainQuery, stub)

    result = asyncio.run(mock.fetch(_FetchPlainQuery(key='real')))

    assert result is stub
    assert calls == 0


def test_mock_unstubbed_query_falls_through_to_real_handler(app) -> None:
    """An un-stubbed query reaches the real ``QueryExecutor.fetch``."""

    @app.queries
    async def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query.key)

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)

    result = asyncio.run(mock.fetch(_FetchPlainQuery(key='real')))

    assert result == _PlainResult(value='real')


def test_mock_reset_clears_query_stubs(app) -> None:
    """``reset_mock`` drops the stub, so the next fetch hits the real handler."""

    @app.queries
    async def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query.key)

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    mock.stub_query(_FetchPlainQuery, _PlainResult(value='stubbed'))
    mock.reset_mock()

    result = asyncio.run(mock.fetch(_FetchPlainQuery(key='real')))

    assert result.value == 'real'


def test_mock_stubbed_entity_query_honoured_through_resolve(app) -> None:
    """A stubbed ``EntityQuery`` short-circuits its handler even when reached via a
    ``Resolve(_FetchUsers)`` field on a rendered model."""
    db_calls = 0

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        nonlocal db_calls
        db_calls += 1
        return {i: _UserDTO(id=i, name=f'db:u{i}') for i in query.ids}

    class _TeamDTO(BaseModel):
        id: int
        owner: Annotated[_UserDTO | None, Resolve(_FetchUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}]

    mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
    mock.stub_query(
        _FetchUsers,
        {10: _UserDTO(id=10, name='stub:u10'), 20: _UserDTO(id=20, name='stub:u20')},
    )

    results = asyncio.run(mock.fetch(_FetchTeams()))

    assert [row.owner.name for row in results] == ['stub:u10', 'stub:u20']
    assert db_calls == 0
