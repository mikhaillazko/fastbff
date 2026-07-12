"""Tests for :class:`FastBFF` and :class:`QueryRouter` — local registration + include_router merge."""

from dataclasses import dataclass
from typing import Annotated
from typing import Any
from typing import Literal

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import build_transform_annotated
from fastbff.exceptions import QueryRegistrationError
from fastbff.exceptions import TransformerRegistrationError


@dataclass(frozen=True)
class _User:
    id: int
    name: str


def test_bff_app_renders_a_transformer_field() -> None:
    app = FastBFF()

    db_calls: list[frozenset[int]] = []

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        db_calls.append(args.ids)
        return {i: _User(id=i, name=f'u{i}') for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        users = query_executor.fetch(_FetchUsers(ids=batch.ids))
        return users.get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class _TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    def fetch_teams() -> list[dict[str, Any]]:
        return [
            {'id': 1, 'owner': 10},
            {'id': 2, 'owner': 20},
            {'id': 3, 'owner': 10},
        ]

    provide_query_executor = app.finalize()
    query_executor = provide_query_executor()
    results = query_executor.fetch(_FetchTeams())

    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})
    assert [r.owner for r in results] == [
        _User(id=10, name='u10'),
        _User(id=20, name='u20'),
        _User(id=10, name='u10'),
    ]


def test_include_router_merges_queries_and_transformers() -> None:
    router = QueryRouter()

    db_calls: list[frozenset[int]] = []

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @router.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        db_calls.append(args.ids)
        return {i: _User(id=i, name=f'u{i}') for i in args.ids}

    @router.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        users = query_executor.fetch(_FetchUsers(ids=batch.ids))
        return users.get(owner_id)

    # build the field annotation BEFORE include_router — this captures the
    # router's wrapped callable; include_router rewires the DI plumbing in
    # place so the captured wrapper still works through the app's scope.
    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class _TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

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
    def fetch_teams(args: _FetchTeams) -> list[dict[str, int]]:
        return teams_by_type[args.type]

    app = FastBFF()
    app.include_router(router)

    provide_query_executor = app.finalize()
    query_executor = provide_query_executor()

    # Act
    results = query_executor.fetch(_FetchTeams(type='volleyball'))

    # Assert
    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})
    assert [r.owner for r in results] == [
        _User(id=10, name='u10'),
        _User(id=20, name='u20'),
        _User(id=10, name='u10'),
    ]


def test_include_router_raises_on_duplicate_query_type() -> None:
    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    router = QueryRouter()

    @router.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        return {}

    app = FastBFF()

    @app.queries
    def fetch_users_again(args: _FetchUsers) -> dict[int, _User]:
        return {}

    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        app.include_router(router)


def test_include_router_raises_on_duplicate_function() -> None:
    router = QueryRouter()
    app = FastBFF()

    def fetch_users(ids: frozenset[int]) -> dict[int, _User]:
        return {}

    router.queries(fetch_users)
    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        app.queries(fetch_users)
        app.include_router(router)


def test_query_annotations_is_read_only_view() -> None:
    """``app.query_annotations`` must not let callers mutate the live registry."""
    # Arrange
    app = FastBFF()

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
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

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    # Act
    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        return {}

    # Assert
    assert _FetchUsers in annotations


def test_include_router_raises_on_duplicate_transformer() -> None:
    """Mirrors the queries-side check: a transformer registered on both the
    router and the app must surface as a loud composition-time error rather
    than silently overwriting the previous registration.
    """
    # Arrange
    router = QueryRouter()
    app = FastBFF()

    def transform_owner(owner_id: int) -> int:
        return owner_id

    router.transformer(transform_owner)
    app.transformer(transform_owner)

    # Act & Assert
    with pytest.raises(TransformerRegistrationError, match='Duplicate @transformer registration'):
        app.include_router(router)


def test_router_dependencies_resolve_through_app_after_include() -> None:
    """A router-registered transformer should pick up the app's bind() overrides."""

    @dataclass(frozen=True)
    class _Greeting:
        message: str

    class _Greeter:
        def hello(self, name: str) -> _Greeting:
            return _Greeting(message=f'plain hello {name}')

    class _StubGreeter:
        def hello(self, name: str) -> _Greeting:
            return _Greeting(message=f'stub hello {name}')

    router = QueryRouter()

    @router.transformer
    def transform_name(
        name: str,
        greeter: Annotated[_Greeter, Depends(_Greeter)],
    ) -> _Greeting:
        return greeter.hello(name)

    GreetingTransformerAnnotated = build_transform_annotated(transform_name)

    class _NameDTO(BaseModel):
        model_config = {'arbitrary_types_allowed': True}
        greeting: GreetingTransformerAnnotated

    app = FastBFF()
    app.bind(_Greeter, lambda: _StubGreeter())
    app.include_router(router)

    class _FetchOne(Query[_NameDTO]):
        pass

    @app.queries(_FetchOne)
    def fetch_one() -> dict[str, str]:
        return {'greeting': 'world'}

    fastapi_app = FastAPI()

    @fastapi_app.get('/one')
    def render_one(
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> dict[str, str]:
        result = query_executor.fetch(_FetchOne())
        return {'greeting': result.greeting.message}

    app.mount(fastapi_app)
    response = TestClient(fastapi_app).get('/one')

    assert response.status_code == 200
    assert response.json() == {'greeting': 'stub hello world'}
