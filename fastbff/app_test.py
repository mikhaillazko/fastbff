"""Tests for :class:`FastBFF` and :class:`QueryRouter` — local registration,
``include_router`` merge, ``bind``/``mount`` DI wiring, and finalize behaviour.

``QueryExecutor.fetch`` is a coroutine, so scenarios that dispatch a query drive
it with ``asyncio.run`` — no pytest-asyncio plugin required. Resolvers are
discovered from the ``Resolve`` fields of registered queries' response models,
so composition needs no decorator.
"""

import asyncio
from typing import Annotated
from typing import Literal

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastbff import EntityQuery
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import Resolve
from fastbff import SyncQueryExecutor
from fastbff.exceptions import QueryNotRegisteredError
from fastbff.exceptions import QueryRegistrationError
from fastbff.exceptions import ResolveRegistrationError

# Module level so get_type_hints resolves them from handler/resolver closures.


class _UserDTO(BaseModel):
    id: int
    name: str = ''


class _FetchUsers(EntityQuery[int, _UserDTO]):
    ids: frozenset[int]


def test_bff_app_renders_a_resolve_field() -> None:
    # Arrange
    app = FastBFF()
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
        return [
            {'id': 1, 'owner': 10},
            {'id': 2, 'owner': 20},
            {'id': 3, 'owner': 10},
        ]

    # Act
    query_executor = app.finalize()()
    results = asyncio.run(query_executor.fetch(_FetchTeams()))

    # Assert — one bulk fetch for the deduped owner ids, values fanned back out.
    assert db_calls == [frozenset({10, 20})]
    assert [row.owner for row in results] == [
        _UserDTO(id=10, name='u10'),
        _UserDTO(id=20, name='u20'),
        _UserDTO(id=10, name='u10'),
    ]


def test_include_router_merges_queries() -> None:
    # Arrange
    router = QueryRouter()
    db_calls: list[frozenset[int]] = []

    @router.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        db_calls.append(query.ids)
        return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}

    class _TeamDTO(BaseModel):
        id: int
        owner: Annotated[_UserDTO | None, Resolve(_FetchUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        type: Literal['volleyball', 'football', 'basketball']

    teams_by_type: dict[str, list[dict[str, int]]] = {
        'volleyball': [
            {'id': 1, 'owner': 10},
            {'id': 2, 'owner': 20},
            {'id': 3, 'owner': 10},
        ],
        'football': [{'id': 4, 'owner': 30}],
        'basketball': [{'id': 5, 'owner': 40}],
    }

    @router.queries
    async def fetch_teams(query: _FetchTeams) -> list[dict[str, int]]:
        return teams_by_type[query.type]

    app = FastBFF()
    app.include_router(router)

    # Act
    query_executor = app.finalize()()
    results = asyncio.run(query_executor.fetch(_FetchTeams(type='volleyball')))

    # Assert — the resolver discovered from _TeamDTO runs through the app's scope.
    assert db_calls == [frozenset({10, 20})]
    assert [row.owner for row in results] == [
        _UserDTO(id=10, name='u10'),
        _UserDTO(id=20, name='u20'),
        _UserDTO(id=10, name='u10'),
    ]


def test_include_router_raises_on_duplicate_query_type() -> None:
    router = QueryRouter()

    @router.queries
    def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    app = FastBFF()

    @app.queries
    def fetch_users_again(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        app.include_router(router)


def test_include_router_raises_on_duplicate_function() -> None:
    router = QueryRouter()
    app = FastBFF()

    def fetch_users(ids: frozenset[int]) -> dict[int, _UserDTO]:
        return {}

    router.queries(fetch_users)
    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        app.queries(fetch_users)
        app.include_router(router)


def test_query_annotations_is_read_only_view() -> None:
    """``app.query_annotations`` must not let callers mutate the live registry."""
    # Arrange
    app = FastBFF()

    @app.queries
    def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    annotations = app.query_annotations

    # Act & Assert — the live view sees registered queries...
    assert _FetchUsers in annotations

    # ...but cannot be mutated through the property.
    with pytest.raises(TypeError):
        annotations[object()] = None  # type: ignore[index]


def test_query_annotations_view_reflects_later_registrations() -> None:
    """The view is live, not a snapshot — new ``@queries`` show up automatically."""
    # Arrange
    app = FastBFF()
    annotations = app.query_annotations
    assert len(annotations) == 0

    # Act
    @app.queries
    def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    # Assert
    assert _FetchUsers in annotations


def test_finalize_is_idempotent_and_refinalizes_on_new_registration() -> None:
    """``finalize`` caches its factory until a new handler invalidates it."""
    # Arrange
    app = FastBFF()

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    # Act & Assert — repeated finalize returns the same synthesized factory...
    first = app.finalize()
    assert app.finalize() is first

    # ...until a new registration invalidates it, forcing a rebuild.
    class _FetchLeads(EntityQuery[int, _UserDTO]):
        ids: frozenset[int]

    @app.queries
    async def fetch_leads(query: _FetchLeads) -> dict[int, _UserDTO]:
        return {}

    assert app.finalize() is not first


def test_mount_installs_executor_overrides() -> None:
    """``mount`` copies both executor overrides into the FastAPI app so async
    endpoints (``QueryExecutor``) and sync endpoints (``SyncQueryExecutor``)
    resolve through the synthesized factory."""
    # Arrange
    app = FastBFF()

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    fastapi_app = FastAPI()

    # Act
    app.mount(fastapi_app)

    # Assert
    assert QueryExecutor in fastapi_app.dependency_overrides
    assert SyncQueryExecutor in fastapi_app.dependency_overrides


def test_finalize_raises_when_resolve_targets_unregistered_entity_query() -> None:
    """A ``Resolve(QueryType)`` whose target is not a registered ``EntityQuery``
    must blow up at composition time, not at request time."""

    # Arrange
    class _UnregisteredUsers(EntityQuery[int, _UserDTO]):
        ids: frozenset[int]

    class _TeamDTO(BaseModel):
        id: int
        owner: Annotated[_UserDTO | None, Resolve(_UnregisteredUsers)]

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    app = FastBFF()

    @app.queries(_FetchTeams)
    async def fetch_teams() -> list[dict[str, int]]:
        return []

    # Act & Assert
    with pytest.raises(ResolveRegistrationError, match='not a registered EntityQuery'):
        app.finalize()


@pytest.mark.parametrize('bind_after_mount', [False, True])
def test_bind_override_reaches_resolver_through_mount(bind_after_mount: bool) -> None:
    """An ``app.bind`` override for a resolver's ``Depends`` param flows through
    ``mount`` and is picked up when the resolver runs during render."""

    # Arrange
    class _Greeter:
        def hello(self, name: str) -> str:
            return f'plain hello {name}'

    class _StubGreeter:
        def hello(self, name: str) -> str:
            return f'stub hello {name}'

    async def resolve_greeting(
        ids: frozenset[int],
        greeter: Annotated[_Greeter, Depends(_Greeter)],
    ) -> dict[int, str]:
        return {i: greeter.hello(f'n{i}') for i in ids}

    class _NameDTO(BaseModel):
        id: int
        greeting: Annotated[str | None, Resolve(resolver=resolve_greeting)]

    class _FetchNames(Query[list[_NameDTO]]):
        pass

    app = FastBFF()
    if not bind_after_mount:
        app.bind(_Greeter, lambda: _StubGreeter())

    @app.queries(_FetchNames)
    async def fetch_names() -> list[dict[str, int]]:
        return [{'id': 1, 'greeting': 1}]

    fastapi_app = FastAPI()

    @fastapi_app.get('/names')
    async def render_names(
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> dict[str, str | None]:
        rows = await query_executor.fetch(_FetchNames())
        return {'greeting': rows[0].greeting}

    app.mount(fastapi_app)
    if bind_after_mount:
        app.bind(_Greeter, lambda: _StubGreeter())

    # Act
    response = TestClient(fastapi_app).get('/names')

    # Assert — the stub, not the concrete _Greeter, resolved the field.
    assert response.status_code == 200
    assert response.json() == {'greeting': 'stub hello n1'}


def test_bind_updates_multiple_mounts_without_replacing_override_dictionaries() -> None:
    class _Service:
        pass

    alias = Annotated[_Service, Depends(_Service)]
    app = FastBFF()
    apis = [FastAPI(), FastAPI()]
    original_dicts = [api.dependency_overrides for api in apis]

    def unrelated() -> str:
        return 'unrelated'

    for api in apis:
        api.dependency_overrides[unrelated] = unrelated
        app.mount(api)

    def first() -> str:
        return 'first'

    def second() -> str:
        return 'second'

    app.bind(alias, first)
    for api in apis:
        assert api.dependency_overrides[_Service] is first

    app.mount(apis[0])
    app.bind(_Service, second)
    for api, original in zip(apis, original_dicts, strict=True):
        assert api.dependency_overrides is original
        assert api.dependency_overrides[_Service] is second
        assert api.dependency_overrides[unrelated] is unrelated


@pytest.mark.parametrize('executor_type', [QueryExecutor, SyncQueryExecutor])
@pytest.mark.parametrize('bind_after_finalize', [False, True])
def test_explicit_executor_override_survives_finalize_rebuild_and_mount(executor_type, bind_after_finalize) -> None:
    app = FastBFF()
    replacement = executor_type()

    def factory():
        return replacement

    if bind_after_finalize:
        app.finalize()
    app.bind(executor_type, factory)
    app.finalize()

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    api = FastAPI()
    app.mount(api)
    app.mount(api)

    @api.get('/')
    async def endpoint(executor: Annotated[executor_type, Depends(executor_type)]) -> bool:
        return executor is replacement

    with TestClient(api) as client:
        assert client.get('/').json() is True
    assert app.dependency_overrides[executor_type] is factory
    assert api.dependency_overrides[executor_type] is factory


def test_direct_executor_override_survives_refinalization() -> None:
    app = FastBFF()
    app.finalize()

    def replacement() -> QueryExecutor:
        return QueryExecutor()

    app.dependency_overrides[QueryExecutor] = replacement

    @app.queries
    async def fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
        return {}

    app.finalize()
    assert app.dependency_overrides[QueryExecutor] is replacement


def test_new_registrations_require_remount_and_do_not_change_old_provider_snapshots() -> None:
    app = FastBFF()
    api = FastAPI()
    old_provider = app.mount(api)
    old_executor = old_provider()

    async def greeting() -> str:
        return 'new dependency'

    class _FetchGreeting(Query[str]):
        pass

    router = QueryRouter()

    @router.queries(_FetchGreeting)
    async def fetch_greeting(value: Annotated[str, Depends(greeting)]) -> str:
        return value

    app.include_router(router)

    @api.get('/')
    async def endpoint(executor: Annotated[QueryExecutor, Depends(QueryExecutor)]) -> str:
        return await executor.fetch(_FetchGreeting())

    with pytest.raises(QueryNotRegisteredError):
        asyncio.run(old_executor.fetch(_FetchGreeting()))
    with pytest.raises(QueryNotRegisteredError):
        asyncio.run(old_provider().fetch(_FetchGreeting()))

    # Finalizing alone does not replace the graph installed on the host app.
    new_provider = app.finalize()
    assert new_provider is not old_provider
    assert api.dependency_overrides[QueryExecutor] is old_provider
    assert app.mount(api) is new_provider
    with TestClient(api) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert response.json() == 'new dependency'


def test_bind_executor_after_mount_reaches_both_async_and_sync_facades() -> None:
    app = FastBFF()
    api = FastAPI()
    app.mount(api)
    replacement = QueryExecutor()
    app.bind(QueryExecutor, lambda: replacement)

    @api.get('/')
    async def endpoint(
        executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
        sync: Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)],
    ) -> bool:
        return executor is replacement and sync._inner is replacement

    with TestClient(api) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert response.json() is True
