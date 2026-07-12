"""Tests for ``QueryExecutor.fetch`` — call-level and entity-level caching,
async/sync handler dispatch, the render pipeline, and the ``SyncQueryExecutor``
facade.

``fetch`` is a coroutine, so each scenario drives it with ``asyncio.run`` — no
pytest-asyncio plugin required. Sync handlers are offloaded to a worker thread
inside ``fetch``; the tests assert observable behaviour, not threading.
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

from fastbff import EntityQuery
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import Resolve
from fastbff import SyncQueryExecutor
from fastbff.exceptions import QueryNotRegisteredError

# ---------------------------------------------------------------------------
# Shared types (module level so get_type_hints resolves them from closures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PlainResult:
    value: str


@dataclass(frozen=True)
class _Entity:
    value: str


class _UserDTO(BaseModel):
    id: int
    name: str = ''


class _FetchPlainQuery(Query[_PlainResult]):
    key: str


class _FetchEntitiesQuery(EntityQuery[int, _Entity]):
    ids: frozenset[int]


class _FetchTenantEntitiesQuery(EntityQuery[int, _Entity]):
    ids: frozenset[int]
    tenant_id: int


class _FetchUsers(EntityQuery[int, _UserDTO]):
    ids: frozenset[int]


async def _resolve_lead(ids: frozenset[int], executor: QueryExecutor) -> dict[int, _UserDTO]:
    """Resolver form: fan the collected ids into the FetchUsers entity query."""
    return await executor.fetch(_FetchUsers(ids=ids))


def _entity_spy() -> MagicMock:
    """A spy that returns one _Entity per requested id."""
    return MagicMock(side_effect=lambda ids: {i: _Entity(value=f'e:{i}') for i in ids})


# ---------------------------------------------------------------------------
# fetch() — call-level caching
# ---------------------------------------------------------------------------


def test_fetch_call_level_caches(app, query_executor) -> None:
    spy = MagicMock(side_effect=lambda request: _PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        return spy(request=query)

    async def scenario() -> tuple[_PlainResult, _PlainResult]:
        first = await query_executor.fetch(_FetchPlainQuery(key='a'))
        second = await query_executor.fetch(_FetchPlainQuery(key='a'))
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second == _PlainResult(value='a')
    spy.assert_called_once()


def test_fetch_different_query_fields_each_fetched(app, query_executor) -> None:
    spy = MagicMock(side_effect=lambda request: _PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        return spy(request=query)

    async def scenario() -> tuple[_PlainResult, _PlainResult]:
        return (
            await query_executor.fetch(_FetchPlainQuery(key='a')),
            await query_executor.fetch(_FetchPlainQuery(key='b')),
        )

    result_a, result_b = asyncio.run(scenario())

    assert result_a.value == 'a'
    assert result_b.value == 'b'
    assert spy.call_count == 2


def test_concurrent_identical_fetch_dedups(app, query_executor) -> None:
    """Two concurrent fetches of the same key share one in-flight future — the
    handler runs once."""
    calls = 0

    @app.queries
    async def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _PlainResult(value=query.key)

    async def scenario() -> list[_PlainResult]:
        return await asyncio.gather(
            query_executor.fetch(_FetchPlainQuery(key='a')),
            query_executor.fetch(_FetchPlainQuery(key='a')),
        )

    results = asyncio.run(scenario())

    assert results == [_PlainResult(value='a'), _PlainResult(value='a')]
    assert calls == 1


# ---------------------------------------------------------------------------
# fetch() — entity-level caching (EntityQuery)
# ---------------------------------------------------------------------------


def test_fetch_entity_first_call_fetches_all(app, query_executor) -> None:
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query.ids)

    result = asyncio.run(query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3}))))

    assert set(result.keys()) == {1, 2, 3}
    spy.assert_called_once()


def test_fetch_entity_same_ids_not_refetched(app, query_executor) -> None:
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query.ids)

    async def scenario() -> None:
        await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
        spy.reset_mock()
        await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    asyncio.run(scenario())

    spy.assert_not_called()


def test_fetch_entity_overlapping_fetches_only_missing(app, query_executor) -> None:
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query.ids)

    async def scenario() -> dict[int, _Entity]:
        await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
        spy.reset_mock()
        return await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({2, 3, 4})))

    result = asyncio.run(scenario())

    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(ids=frozenset({4}))


def test_fetch_absent_ids_excluded_and_not_refetched(app, query_executor) -> None:
    """Backend returns only id 1; absent ids 2/3 are remembered, so an overlapping
    request only fetches the genuinely-new id."""
    spy = MagicMock(side_effect=lambda ids: {i: _Entity(value=f'e:{i}') for i in ids if i == 1})

    @app.queries
    def fetch_entities(query: _FetchEntitiesQuery) -> dict[int, _Entity]:
        return spy(ids=query.ids)

    async def scenario() -> tuple[dict[int, _Entity], dict[int, _Entity]]:
        first = await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
        second = await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({2, 3, 4})))
        return first, second

    first, second = asyncio.run(scenario())

    assert set(first.keys()) == {1}
    assert second == {}
    spy.assert_called_with(ids=frozenset({4}))
    assert spy.call_count == 2


def test_fetch_absence_is_per_executor(app, query_executor) -> None:
    """Absence is cached per-request; a fresh executor re-fetches."""
    call_args: list[frozenset[int]] = []

    @app.queries
    def fetch_entities(query: _FetchEntitiesQuery) -> dict[int, _Entity]:
        call_args.append(query.ids)
        return {}

    async def scenario() -> None:
        await query_executor.fetch(_FetchEntitiesQuery(ids=frozenset({1})))
        fresh = QueryExecutor.create(query_annotations=app.query_annotations)
        await fresh.fetch(_FetchEntitiesQuery(ids=frozenset({1})))

    asyncio.run(scenario())

    assert len(call_args) == 2


def test_fetch_entity_discriminating_field_does_not_share_bucket(app, query_executor) -> None:
    """An entity query with a field beyond its ids (``tenant_id``) must not
    cross-serve cached entries between different values of that field."""
    seen: list[tuple[int, frozenset[int]]] = []

    @app.queries
    def fetch_tenant_entities(query: _FetchTenantEntitiesQuery) -> dict[int, _Entity]:
        seen.append((query.tenant_id, query.ids))
        return {i: _Entity(value=f't{query.tenant_id}:{i}') for i in query.ids}

    async def scenario() -> tuple[dict[int, _Entity], dict[int, _Entity]]:
        return (
            await query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({1, 2}), tenant_id=1)),
            await query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({1, 2}), tenant_id=2)),
        )

    tenant_1, tenant_2 = asyncio.run(scenario())

    assert tenant_1 == {1: _Entity(value='t1:1'), 2: _Entity(value='t1:2')}
    assert tenant_2 == {1: _Entity(value='t2:1'), 2: _Entity(value='t2:2')}
    assert seen == [(1, frozenset({1, 2})), (2, frozenset({1, 2}))]


def test_fetch_entity_same_discriminating_field_shares_bucket(app, query_executor) -> None:
    spy = MagicMock(side_effect=lambda tenant_id, ids: {i: _Entity(value=f't{tenant_id}:{i}') for i in ids})

    @app.queries
    def fetch_tenant_entities(query: _FetchTenantEntitiesQuery) -> dict[int, _Entity]:
        return spy(tenant_id=query.tenant_id, ids=query.ids)

    async def scenario() -> dict[int, _Entity]:
        await query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({1, 2, 3}), tenant_id=1))
        spy.reset_mock()
        return await query_executor.fetch(_FetchTenantEntitiesQuery(ids=frozenset({2, 3, 4}), tenant_id=1))

    result = asyncio.run(scenario())

    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(tenant_id=1, ids=frozenset({4}))


def test_plain_dict_query_is_not_entity_cached(app, query_executor) -> None:
    """A plain ``Query[dict[K, V]]`` (not an EntityQuery) gets call-level caching
    only — no per-id sharing, even with an iterable field."""

    class _PlainDictQuery(Query[dict[int, _Entity]]):
        ids: frozenset[int]

    spy = _entity_spy()

    @app.queries
    def fetch_plain_dict(query: _PlainDictQuery) -> dict[int, _Entity]:
        return spy(ids=query.ids)

    async def scenario() -> None:
        await query_executor.fetch(_PlainDictQuery(ids=frozenset({1, 2, 3})))
        # A subset would be served from the entity cache if this were an
        # EntityQuery; a plain query re-fetches (different call key).
        await query_executor.fetch(_PlainDictQuery(ids=frozenset({1, 2})))

    asyncio.run(scenario())

    assert spy.call_count == 2


# ---------------------------------------------------------------------------
# Render pipeline (Resolve)
# ---------------------------------------------------------------------------


def test_render_resolve_query_form_one_bulk_fetch(app) -> None:
    """``Resolve(FetchUsers)`` collects owner ids across the page and issues one
    bulk entity fetch; overlapping ids collapse."""
    db_calls: list[frozenset[int]] = []

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        db_calls.append(query.ids)
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}

    class _TeamDTO(BaseModel):
        id: int
        owner: Annotated[_UserDTO | None, Resolve(_FetchUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    executor = app.finalize()()
    results = asyncio.run(executor.fetch(_FetchTeams()))

    assert db_calls == [frozenset({10, 20})]
    assert [row.owner.id for row in results] == [10, 20, 10]


def test_render_resolver_form(app) -> None:
    """``Resolve(resolver=fn)`` invokes the resolver with the collected ids plus
    the executor injected by type."""

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}

    class _TeamDTO(BaseModel):
        id: int
        lead: Annotated[_UserDTO | None, Resolve(resolver=_resolve_lead)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'lead': 10}, {'id': 2, 'lead': 20}]

    executor = app.finalize()()
    results = asyncio.run(executor.fetch(_FetchTeams()))

    assert [row.lead.id for row in results] == [10, 20]


def test_render_absent_resolve_becomes_none(app) -> None:
    """An id with no backing entity resolves to None (field is optional)."""

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids if i != 99}

    class _TeamDTO(BaseModel):
        id: int
        owner: Annotated[_UserDTO | None, Resolve(_FetchUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 99}]

    executor = app.finalize()()
    results = asyncio.run(executor.fetch(_FetchTeams()))

    assert results[0].owner == _UserDTO(id=10, name='u10')
    assert results[1].owner is None


def test_render_reuses_entity_cache_for_direct_fetch(app) -> None:
    """Ids fetched during render populate the entity cache, so a later direct
    fetch of an already-seen id issues no new backend call."""
    db_calls: list[frozenset[int]] = []

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        db_calls.append(query.ids)
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}

    class _TeamDTO(BaseModel):
        id: int
        owner: Annotated[_UserDTO | None, Resolve(_FetchUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}]

    executor = app.finalize()()

    async def scenario() -> _UserDTO | None:
        await executor.fetch(_FetchTeams())
        users = await executor.fetch(_FetchUsers(ids=frozenset({10})))
        return users.get(10)

    user = asyncio.run(scenario())

    assert user == _UserDTO(id=10, name='u10')
    assert db_calls == [frozenset({10})]  # id 10 fetched once, reused


def test_render_collection_resolve_field(app) -> None:
    """A collection resolve field (``list[UserDTO]``) resolves each id in the raw
    list; the whole page's ids collapse into one bulk fetch."""
    db_calls: list[frozenset[int]] = []

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        db_calls.append(query.ids)
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}

    class _TeamDTO(BaseModel):
        id: int
        members: Annotated[list[_UserDTO], Resolve(_FetchUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, Any]]:
        return [{'id': 1, 'members': [10, 20]}, {'id': 2, 'members': [20, 30]}]

    executor = app.finalize()()
    results = asyncio.run(executor.fetch(_FetchTeams()))

    assert db_calls == [frozenset({10, 20, 30})]
    assert [[member.id for member in row.members] for row in results] == [[10, 20], [20, 30]]


def test_render_nested_model_field(app) -> None:
    """A field typed as another resolve-bearing model resolves depth-first, with the
    nested model's ids batched across the whole page (one bulk fetch)."""
    db_calls: list[frozenset[int]] = []

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        db_calls.append(query.ids)
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}

    class _MemberDTO(BaseModel):
        role: str
        user: Annotated[_UserDTO | None, Resolve(_FetchUsers)]

    class _TeamDTO(BaseModel):
        id: int
        lead: _MemberDTO

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, Any]]:
        return [
            {'id': 1, 'lead': {'role': 'owner', 'user': 10}},
            {'id': 2, 'lead': {'role': 'owner', 'user': 20}},
            {'id': 3, 'lead': {'role': 'owner', 'user': 10}},
        ]

    executor = app.finalize()()
    results = asyncio.run(executor.fetch(_FetchTeams()))

    # Nested user ids across the page collapse into a single bulk fetch.
    assert db_calls == [frozenset({10, 20})]
    assert [row.lead.user.id for row in results] == [10, 20, 10]
    assert [row.lead.role for row in results] == ['owner', 'owner', 'owner']


# ---------------------------------------------------------------------------
# Async composition + concurrency
# ---------------------------------------------------------------------------


def test_async_composition_does_not_exhaust_pool(app) -> None:
    """Async handlers that compose via nested ``fetch`` run loop-native, so deep
    composition never grabs a worker thread per level and deadlocks a small pool.
    """

    class _Leaf(Query[_PlainResult]):
        n: int

    class _Chain(Query[_PlainResult]):
        depth: int

    @app.queries
    async def leaf(query: _Leaf) -> _PlainResult:
        return _PlainResult(value=f'leaf:{query.n}')

    @app.queries
    async def chain(
        query: _Chain,
        executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _PlainResult:
        if query.depth == 0:
            return await executor.fetch(_Leaf(n=0))
        return await executor.fetch(_Chain(depth=query.depth - 1))

    executor = app.finalize()()

    async def scenario() -> _PlainResult:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        with anyio.fail_after(5):
            return await executor.fetch(_Chain(depth=8))

    assert asyncio.run(scenario()) == _PlainResult(value='leaf:0')


def test_concurrent_fetch_is_cache_safe(app, query_executor) -> None:
    """``asyncio.gather`` over independent queries shares one cache — results
    stay correct."""

    class _FetchA(Query[_PlainResult]):
        key: str

    class _FetchB(Query[_PlainResult]):
        key: str

    @app.queries
    async def fetch_a(query: _FetchA) -> _PlainResult:
        await asyncio.sleep(0)
        return _PlainResult(value=f'a:{query.key}')

    @app.queries
    async def fetch_b(query: _FetchB) -> _PlainResult:
        await asyncio.sleep(0)
        return _PlainResult(value=f'b:{query.key}')

    async def scenario() -> list[_PlainResult]:
        return await asyncio.gather(
            query_executor.fetch(_FetchA(key='a')),
            query_executor.fetch(_FetchB(key='b')),
            query_executor.fetch(_FetchA(key='a')),
        )

    results = asyncio.run(scenario())

    assert results == [
        _PlainResult(value='a:a'),
        _PlainResult(value='b:b'),
        _PlainResult(value='a:a'),
    ]


# ---------------------------------------------------------------------------
# SyncQueryExecutor facade
# ---------------------------------------------------------------------------


def test_sync_query_executor_bridges_async_handler(app) -> None:
    """``SyncQueryExecutor.fetch`` (called from a worker thread, as a sync endpoint
    would) bridges onto the loop and reaches an async handler."""

    @app.queries
    async def fetch_plain(query: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query.key)

    async def scenario() -> _PlainResult:
        inner = QueryExecutor.create(query_annotations=app.query_annotations)
        sync_executor = SyncQueryExecutor.create(inner)
        return await anyio.to_thread.run_sync(sync_executor.fetch, _FetchPlainQuery(key='a'))

    assert asyncio.run(scenario()) == _PlainResult(value='a')


# ---------------------------------------------------------------------------
# DI-facing empty signatures
# ---------------------------------------------------------------------------


def test_executors_have_empty_signature() -> None:
    """Endpoints declare ``Depends(QueryExecutor)`` / ``Depends(SyncQueryExecutor)``,
    so FastAPI introspects each class's signature at startup. A parameterless
    ``__init__`` keeps it empty — no params leak in as request fields; the
    mount-time override supplies the real instance."""
    assert list(signature(QueryExecutor).parameters) == []
    assert list(signature(SyncQueryExecutor).parameters) == []


def test_fetch_unregistered_query_raises(query_executor) -> None:
    class _Unregistered(Query[_PlainResult]):
        key: str

    async def scenario() -> Any:
        return await query_executor.fetch(_Unregistered(key='a'))

    with pytest.raises(QueryNotRegisteredError):
        asyncio.run(scenario())
