"""Async dispatch — ``QueryExecutor.afetch`` bridges ``async def`` handlers and
transformers onto the running event loop from a worker thread.

These are written as plain sync tests that drive the async surface with
``asyncio.run`` so no pytest-asyncio plugin is required.
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated

import anyio.to_thread
import pytest
from fastapi import Depends
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import build_transform_annotated
from fastbff.exceptions import AsyncDispatchError


@dataclass(frozen=True)
class _User:
    id: int


@dataclass(frozen=True)
class PlainResult:
    value: str


class FetchPlainQuery(Query[PlainResult]):
    key: str


def test_afetch_runs_async_handler(app, query_executor) -> None:
    """A bare ``async def`` handler is awaited and its result returned."""

    @app.queries
    async def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return PlainResult(value=query_args.key)

    result = asyncio.run(query_executor.afetch(FetchPlainQuery(key='a')))

    assert result == PlainResult(value='a')


def test_afetch_caches_async_handler(app, query_executor) -> None:
    """Awaited results land in the call cache like sync ones — handler runs once."""
    calls = 0

    @app.queries
    async def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        nonlocal calls
        calls += 1
        return PlainResult(value=query_args.key)

    async def scenario() -> tuple[PlainResult, PlainResult]:
        first = await query_executor.afetch(FetchPlainQuery(key='a'))
        second = await query_executor.afetch(FetchPlainQuery(key='a'))
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second == PlainResult(value='a')
    assert calls == 1


def test_sync_fetch_of_async_handler_in_pure_sync_context_raises(app, query_executor) -> None:
    """No worker-thread portal to bridge through → clear error, not a bare coroutine."""

    @app.queries
    async def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return PlainResult(value=query_args.key)

    with pytest.raises(AsyncDispatchError, match='afetch'):
        query_executor.fetch(FetchPlainQuery(key='a'))


def test_sync_fetch_of_async_handler_bridges_in_worker_thread(app, query_executor) -> None:
    """The FastAPI-native path: a *sync* endpoint runs in Starlette's worker thread,
    so sync ``fetch`` can bridge an async handler onto the loop. Simulated here by
    running ``fetch`` via ``anyio.to_thread.run_sync`` (what Starlette does)."""

    @app.queries
    async def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return PlainResult(value=query_args.key)

    async def scenario() -> PlainResult:
        return await anyio.to_thread.run_sync(query_executor.fetch, FetchPlainQuery(key='a'))

    assert asyncio.run(scenario()) == PlainResult(value='a')


def test_afetch_async_handler_through_transformer() -> None:
    """The N+1 case: a sync transformer fetches an *async* handler, driven by afetch.

    One bulk fetch for the whole page; the async ``fetch_users`` coroutine is
    bridged onto the loop from the validation worker thread.
    """
    app = FastBFF()
    db_calls: list[frozenset[int]] = []

    class FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    async def fetch_users(args: FetchUsers) -> dict[int, _User]:
        db_calls.append(args.ids)
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(FetchUsers(ids=batch.ids)).get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

    class FetchTeams(Query[list[TeamDTO]]):
        pass

    @app.queries(FetchTeams)
    def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    query_executor = app.finalize()()
    results = asyncio.run(query_executor.afetch(FetchTeams()))

    assert db_calls == [frozenset({10, 20})]  # single bulk fetch
    assert [row.owner for row in results] == [_User(id=10), _User(id=20), _User(id=10)]


def test_afetch_async_transformer() -> None:
    """An ``async def`` transformer is itself bridged onto the loop."""
    app = FastBFF()

    class FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: FetchUsers) -> dict[int, _User]:
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    async def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(FetchUsers(ids=batch.ids)).get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

    class FetchTeams(Query[list[TeamDTO]]):
        pass

    @app.queries(FetchTeams)
    def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}]

    query_executor = app.finalize()()
    results = asyncio.run(query_executor.afetch(FetchTeams()))

    assert [row.owner for row in results] == [_User(id=10), _User(id=20)]


def test_concurrent_afetch_is_cache_safe(app, query_executor) -> None:
    """``asyncio.gather`` over independent async queries runs on parallel worker
    threads sharing one cache — results must be correct (thread-safety smoke)."""

    class FetchA(Query[PlainResult]):
        key: str

    class FetchB(Query[PlainResult]):
        key: str

    @app.queries
    async def fetch_a(query_args: FetchA) -> PlainResult:
        await asyncio.sleep(0)
        return PlainResult(value=f'a:{query_args.key}')

    @app.queries
    async def fetch_b(query_args: FetchB) -> PlainResult:
        await asyncio.sleep(0)
        return PlainResult(value=f'b:{query_args.key}')

    async def scenario() -> list[PlainResult]:
        return await asyncio.gather(
            query_executor.afetch(FetchA(key='x')),
            query_executor.afetch(FetchB(key='y')),
            query_executor.afetch(FetchA(key='x')),
        )

    results = asyncio.run(scenario())

    assert results == [
        PlainResult(value='a:x'),
        PlainResult(value='b:y'),
        PlainResult(value='a:x'),
    ]
