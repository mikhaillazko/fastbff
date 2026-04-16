"""Tests for :class:`FastBFF` and :class:`QueryRouter` — local registration + include_router merge."""

from dataclasses import dataclass
from typing import Annotated

import pytest
from fastapi import Depends
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import build_transform_annotated
from fastbff.exceptions import QueryRegistrationError


@dataclass(frozen=True)
class User:
    id: int
    name: str


def test_bff_app_renders_a_transformer_field() -> None:
    app = FastBFF()

    db_calls: list[frozenset[int]] = []

    class FetchUsers(Query[dict[int, User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: FetchUsers) -> dict[int, User]:
        db_calls.append(args.ids)
        return {i: User(id=i, name=f'u{i}') for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> User | None:
        users = query_executor.fetch(FetchUsers(ids=batch.ids))
        return users.get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

    rows: list[dict[str, int]] = [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},
    ]

    @app.injector.entrypoint
    def render_page() -> list[TeamDTO]:
        return app.executor.render(TeamDTO, rows)

    results = render_page()

    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})
    assert [r.owner for r in results] == [
        User(id=10, name='u10'),
        User(id=20, name='u20'),
        User(id=10, name='u10'),
    ]


def test_include_router_merges_queries_and_transformers() -> None:
    router = QueryRouter()

    db_calls: list[frozenset[int]] = []

    class FetchUsers(Query[dict[int, User]]):
        ids: frozenset[int]

    @router.queries
    def fetch_users(args: FetchUsers) -> dict[int, User]:
        db_calls.append(args.ids)
        return {i: User(id=i, name=f'u{i}') for i in args.ids}

    @router.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> User | None:
        users = query_executor.fetch(FetchUsers(ids=batch.ids))
        return users.get(owner_id)

    # build the field annotation BEFORE include_router — this captures the
    # router's wrapped callable; include_router rewires the DI plumbing in
    # place so the captured wrapper still works through the app's scope.
    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

    app = FastBFF()
    app.include_router(router)

    @app.injector.entrypoint
    def render_page() -> list[TeamDTO]:
        return app.executor.render(TeamDTO, [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}])

    results = render_page()

    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})
    assert results[0].owner == User(id=10, name='u10')
    assert results[1].owner == User(id=20, name='u20')


def test_include_router_raises_on_duplicate_query_type() -> None:
    class FetchUsers(Query[dict[int, User]]):
        ids: frozenset[int]

    router = QueryRouter()

    @router.queries
    def fetch_users(args: FetchUsers) -> dict[int, User]:
        return {}

    app = FastBFF()

    @app.queries
    def fetch_users_again(args: FetchUsers) -> dict[int, User]:
        return {}

    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        app.include_router(router)


def test_include_router_raises_on_duplicate_function() -> None:
    router = QueryRouter()
    app = FastBFF()

    def fetch_users(ids: frozenset[int]) -> dict[int, User]:
        return {}

    router.queries(fetch_users)
    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        app.queries(fetch_users)
        app.include_router(router)


def test_router_dependencies_resolve_through_app_after_include() -> None:
    """A router-registered transformer should pick up the app's bind() overrides."""

    @dataclass(frozen=True)
    class Greeting:
        message: str

    class Greeter:
        def hello(self, name: str) -> Greeting:
            return Greeting(message=f'plain hello {name}')

    class StubGreeter:
        def hello(self, name: str) -> Greeting:
            return Greeting(message=f'stub hello {name}')

    router = QueryRouter()

    @router.transformer
    def transform_name(
        name: str,
        greeter: Annotated[Greeter, Depends(Greeter)],
    ) -> Greeting:
        return greeter.hello(name)

    GreetingTransformerAnnotated = build_transform_annotated(transform_name)

    class NameDTO(BaseModel):
        model_config = {'arbitrary_types_allowed': True}
        greeting: GreetingTransformerAnnotated

    app = FastBFF()
    app.bind(Greeter, lambda: StubGreeter())
    app.include_router(router)

    @app.injector.entrypoint
    def render_one() -> NameDTO:
        return app.executor.render(NameDTO, [{'greeting': 'world'}])[0]

    result = render_one()
    assert result.greeting == Greeting(message='stub hello world')
