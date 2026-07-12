"""Tests for ``QueryExecutor.fetch`` / ``afetch`` — call-level and entity-level
caching, plus ``async def`` handler/transformer dispatch.

The async tests are written as plain sync tests that drive the async surface
with ``asyncio.run`` so no pytest-asyncio plugin is required.
"""

import asyncio
from dataclasses import dataclass
from inspect import signature
from typing import Annotated
from typing import Any
from unittest.mock import MagicMock

import anyio.to_thread
import pytest
from fastapi import Depends
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import build_transform_annotated
from fastbff.exceptions import AsyncDispatchError
from fastbff.query_executor.query import Query
from fastbff.query_executor.query_executor import QueryExecutor

# ---------------------------------------------------------------------------
# Shared return types
# (declared at module level so get_type_hints can resolve them in closures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PlainResult:
    value: str


@dataclass(frozen=True)
class _Entity:
    value: str


@dataclass(frozen=True)
class _User:
    id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity_spy() -> MagicMock:
    """A spy that returns one _Entity per requested id."""
    return MagicMock(side_effect=lambda ids: {i: _Entity(value=f'e:{i}') for i in ids})


# ---------------------------------------------------------------------------
# Query objects
# ---------------------------------------------------------------------------


class _FetchPlainQuery(Query[_PlainResult]):
    key: str


class _FetchEntitiesQuery(Query[dict[int, _Entity]]):
    ids: frozenset[int]


class _FetchTenantEntitiesQuery(Query[dict[int, _Entity]]):
    ids: frozenset[int]
    tenant_id: int


# ---------------------------------------------------------------------------
# fetch() — call-level caching
# ---------------------------------------------------------------------------


def test_fetch_call_level_caches(app, query_executor) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: _PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return spy(request=query_args)

    # Act
    result_1 = query_executor.fetch(_FetchPlainQuery(key='a'))
    result_2 = query_executor.fetch(_FetchPlainQuery(key='a'))

    # Assert
    assert result_1 == result_2
    spy.assert_called_once()


def test_fetch_different_query_fields_each_fetched(app, query_executor) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: _PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return spy(request=query_args)

    # Act
    result_1 = query_executor.fetch(_FetchPlainQuery(key='a'))
    result_2 = query_executor.fetch(_FetchPlainQuery(key='b'))

    # Assert
    assert result_1.value == 'a'
    assert result_2.value == 'b'
    assert spy.call_count == 2


# ---------------------------------------------------------------------------
# fetch() — entity-level caching (dict return + IDs field)
# ---------------------------------------------------------------------------


def test_fetch_entity_first_call_fetches_all(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query_args.ids)

    # Act
    result = query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    assert set(result.keys()) == {1, 2, 3}
    spy.assert_called_once()


def test_fetch_entity_same_ids_not_refetched(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    spy.assert_not_called()


def test_fetch_entity_subset_not_refetched(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    result = query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2})))

    # Assert
    assert set(result.keys()) == {1, 2}
    spy.assert_not_called()


def test_fetch_entity_overlapping_fetches_only_missing(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    result = query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({2, 3, 4})))

    # Assert
    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(ids=frozenset({4}))


def test_fetch_absent_ids_excluded_from_result(app, query_executor) -> None:
    # Arrange
    spy = MagicMock(return_value={})

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query_args.ids)

    # Act
    result = query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    assert result == {}
    spy.assert_called_once()


def test_fetch_absent_ids_not_refetched_on_overlap(app, query_executor) -> None:
    # Arrange — backend only returns id 1; ids 2 and 3 are absent.
    spy = MagicMock(side_effect=lambda ids: {i: _Entity(value=f'e:{i}') for i in ids if i == 1})

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query_args.ids)

    result_1 = query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    assert set(result_1.keys()) == {1}
    assert spy.call_count == 1

    # Act — overlap on absent ids 2 and 3; only the new id 4 should be fetched.
    result_2 = query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({2, 3, 4})))

    # Assert
    assert result_2 == {}  # 4 is also absent
    spy.assert_called_with(ids=frozenset({4}))
    assert spy.call_count == 2


def test_fetch_absent_id_becomes_present_in_new_executor(app, query_executor) -> None:
    # Arrange — absence is cached per-executor (per-request); a new executor must re-fetch.
    call_args: list[frozenset[int]] = []

    @app.queries
    def fetch_entities(query_args: _FetchEntitiesQuery) -> dict[int, _Entity]:
        call_args.append(query_args.ids)
        return {}

    query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1})))

    # Act
    fresh_executor = QueryExecutor.create(query_annotations=app.query_annotations)
    fresh_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1})))

    # Assert
    assert len(call_args) == 2


def test_fetch_entity_discriminating_field_does_not_share_bucket(app, query_executor) -> None:
    """An entity query with a field beyond its ids (e.g. ``tenant_id``) must not
    cross-serve cached entries between different values of that field."""
    # Arrange — the backend tags each entity with the tenant it was fetched for.
    seen: list[tuple[int, frozenset[int]]] = []

    @app.queries
    def fetch_tenant_entities(query_args: _FetchTenantEntitiesQuery) -> dict[int, _Entity]:
        seen.append((query_args.tenant_id, query_args.ids))
        return {i: _Entity(value=f't{query_args.tenant_id}:{i}') for i in query_args.ids}

    # Act — same ids, different tenants, within one request/executor.
    tenant_1 = query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({1, 2}), tenant_id=1))
    tenant_2 = query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({1, 2}), tenant_id=2))

    # Assert — each tenant is fetched independently and gets its own entities.
    assert tenant_1 == {1: _Entity(value='t1:1'), 2: _Entity(value='t1:2')}
    assert tenant_2 == {1: _Entity(value='t2:1'), 2: _Entity(value='t2:2')}
    assert seen == [(1, frozenset({1, 2})), (2, frozenset({1, 2}))]


def test_fetch_entity_same_discriminating_field_shares_bucket(app, query_executor) -> None:
    """Within the same discriminating value, overlapping id sets still share the
    entity cache — only missing ids are fetched."""
    # Arrange
    spy = MagicMock(side_effect=lambda tenant_id, ids: {i: _Entity(value=f't{tenant_id}:{i}') for i in ids})

    @app.queries
    def fetch_tenant_entities(query_args: _FetchTenantEntitiesQuery) -> dict[int, _Entity]:
        return spy(tenant_id=query_args.tenant_id, ids=query_args.ids)

    query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({1, 2, 3}), tenant_id=1))
    spy.reset_mock()

    # Act — same tenant, overlapping ids.
    result = query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({2, 3, 4}), tenant_id=1))

    # Assert — only the new id 4 hits the backend.
    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(tenant_id=1, ids=frozenset({4}))


# ---------------------------------------------------------------------------
# afetch() — async handlers and transformers (anyio worker-thread bridge)
# ---------------------------------------------------------------------------


def test_afetch_runs_async_handler(app, query_executor) -> None:
    """A bare ``async def`` handler is awaited and its result returned."""

    @app.queries
    async def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query_args.key)

    result = asyncio.run(query_executor.afetch(_FetchPlainQuery(key='a')))

    assert result == _PlainResult(value='a')


def test_afetch_caches_async_handler(app, query_executor) -> None:
    """Awaited results land in the call cache like sync ones — handler runs once."""
    calls = 0

    @app.queries
    async def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        nonlocal calls
        calls += 1
        return _PlainResult(value=query_args.key)

    async def scenario() -> tuple[_PlainResult, _PlainResult]:
        first = await query_executor.afetch(_FetchPlainQuery(key='a'))
        second = await query_executor.afetch(_FetchPlainQuery(key='a'))
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second == _PlainResult(value='a')
    assert calls == 1


def test_sync_fetch_of_async_handler_in_pure_sync_context_raises(app, query_executor) -> None:
    """No worker-thread portal to bridge through → clear error, not a bare coroutine."""

    @app.queries
    async def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query_args.key)

    with pytest.raises(AsyncDispatchError, match='afetch'):
        query_executor.fetch(_FetchPlainQuery(key='a'))


def test_sync_fetch_of_async_handler_bridges_in_worker_thread(app, query_executor) -> None:
    """The FastAPI-native path: a *sync* endpoint runs in Starlette's worker thread,
    so sync ``fetch`` can bridge an async handler onto the loop. Simulated here by
    running ``fetch`` via ``anyio.to_thread.run_sync`` (what Starlette does)."""

    @app.queries
    async def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query_args.key)

    async def scenario() -> _PlainResult:
        return await anyio.to_thread.run_sync(query_executor.fetch, _FetchPlainQuery(key='a'))

    assert asyncio.run(scenario()) == _PlainResult(value='a')


def test_afetch_async_handler_through_transformer() -> None:
    """The N+1 case: a sync transformer fetches an *async* handler, driven by afetch.

    One bulk fetch for the whole page; the async ``fetch_users`` coroutine is
    bridged onto the loop from the validation worker thread.
    """
    app = FastBFF()
    db_calls: list[frozenset[int]] = []

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    async def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        db_calls.append(args.ids)
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    _OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class _TeamDTO(BaseModel):
        id: int
        owner: _OwnerTransformerAnnotated

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    query_executor = app.finalize()()
    results = asyncio.run(query_executor.afetch(_FetchTeams()))

    assert db_calls == [frozenset({10, 20})]  # single bulk fetch
    assert [row.owner for row in results] == [_User(id=10), _User(id=20), _User(id=10)]


def test_afetch_async_transformer() -> None:
    """An ``async def`` transformer is itself bridged onto the loop."""
    app = FastBFF()

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    async def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    _OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class _TeamDTO(BaseModel):
        id: int
        owner: _OwnerTransformerAnnotated

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}]

    query_executor = app.finalize()()
    results = asyncio.run(query_executor.afetch(_FetchTeams()))

    assert [row.owner for row in results] == [_User(id=10), _User(id=20)]


def test_afetch_async_handler_composition_does_not_exhaust_pool() -> None:
    """Async handlers that compose via nested ``afetch`` must run on the loop, not
    each grab a worker thread — otherwise deep composition deadlocks a small pool.

    The pool is shrunk to a single token, far below the composition depth: the
    loop-native path uses no worker threads and completes, whereas the old
    offload-per-level path would exhaust the pool and hang (caught by the
    ``fail_after`` deadline)."""
    app = FastBFF()

    class _Leaf(Query[_PlainResult]):
        n: int

    class _Chain(Query[_PlainResult]):
        depth: int

    @app.queries
    async def leaf(query_args: _Leaf) -> _PlainResult:
        return _PlainResult(value=f'leaf:{query_args.n}')

    @app.queries
    async def chain(
        query_args: _Chain,
        executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _PlainResult:
        if query_args.depth == 0:
            return await executor.afetch(_Leaf(n=0))
        return await executor.afetch(_Chain(depth=query_args.depth - 1))

    query_executor = app.finalize()()

    async def scenario() -> _PlainResult:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        with anyio.fail_after(5):
            return await query_executor.afetch(_Chain(depth=8))

    assert asyncio.run(scenario()) == _PlainResult(value='leaf:0')


def test_call_handler_rejects_undetected_coroutine(query_executor) -> None:
    """A callable that hides an ``async`` body from ``iscoroutinefunction`` (so it
    runs down the sync branch but returns a coroutine) must fail loudly rather
    than cache an unawaited coroutine — the corruption the bridge guards against."""

    async def _inner() -> str:
        return 'a'

    def sneaky() -> Any:  # sync signature, returns a coroutine
        return _inner()

    with pytest.raises(AsyncDispatchError, match='coroutine'):
        query_executor.call_handler(sneaky)


def test_concurrent_afetch_is_cache_safe(app, query_executor) -> None:
    """``asyncio.gather`` over independent async queries runs on parallel worker
    threads sharing one cache — results must be correct (thread-safety smoke)."""

    class _FetchA(Query[_PlainResult]):
        key: str

    class _FetchB(Query[_PlainResult]):
        key: str

    @app.queries
    async def fetch_a(query_args: _FetchA) -> _PlainResult:
        await asyncio.sleep(0)
        return _PlainResult(value=f'a:{query_args.key}')

    @app.queries
    async def fetch_b(query_args: _FetchB) -> _PlainResult:
        await asyncio.sleep(0)
        return _PlainResult(value=f'b:{query_args.key}')

    async def scenario() -> list[_PlainResult]:
        return await asyncio.gather(
            query_executor.afetch(_FetchA(key='a')),
            query_executor.afetch(_FetchB(key='b')),
            query_executor.afetch(_FetchA(key='a')),
        )

    results = asyncio.run(scenario())

    assert results == [
        _PlainResult(value='a:a'),
        _PlainResult(value='b:b'),
        _PlainResult(value='a:a'),
    ]


def test_query_executor_has_empty_signature() -> None:
    """Endpoints declare ``Annotated[QueryExecutor, Depends(QueryExecutor)]``, so
    FastAPI introspects ``inspect.signature(QueryExecutor)`` at startup. The
    parameterless ``__init__`` must keep that signature empty — otherwise
    ``__init__`` params would leak in as request fields. Guards the invariant
    that replaced the former ``QueryExecutor.__signature__ = Signature([])``
    mutation.
    """
    assert list(signature(QueryExecutor).parameters) == []
