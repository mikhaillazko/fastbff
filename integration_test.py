"""End-to-end Plan → Fetch → Merge flow with the simplified one-call API."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel

from pydantic_bff import BatchArg
from pydantic_bff import InjectorRegistry
from pydantic_bff import QueriesRegistry
from pydantic_bff import Query
from pydantic_bff import QueryExecutor
from pydantic_bff import TransformerRegistry
from pydantic_bff import build_transform_annotated


@dataclass(frozen=True)
class User:
    id: int
    name: str


def test_render_issues_one_bulk_call_per_page() -> None:
    # Arrange — wire a real DI container, queries registry, and transformer registry.
    injector = InjectorRegistry()
    queries = QueriesRegistry(injector=injector)  # type: ignore[arg-type]
    transformer = TransformerRegistry(injector=injector)  # type: ignore[arg-type]
    executor = QueryExecutor(queries_registry=queries)  # type: ignore[arg-type]
    injector.bind(QueryExecutor, lambda: executor)

    db_calls: list[frozenset[int]] = []

    class FetchUsers(Query[dict[int, User]]):
        ids: frozenset[int]

    @queries
    def fetch_users(args: FetchUsers) -> dict[int, User]:
        db_calls.append(args.ids)
        return {i: User(id=i, name=f'u{i}') for i in args.ids}

    @transformer(prefetch=FetchUsers)
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> User | None:
        users = query_executor.fetch(FetchUsers(ids=batch.ids))
        return users.get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class TeamDTO(BaseModel):  # no @bff_model — auto-introspected
        id: int
        owner: OwnerTransformerAnnotated

    rows: list[dict[str, int]] = [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},  # duplicate id → still just one DB call
    ]

    # Act — single call orchestrates Plan + Fetch + Merge.
    @injector.entrypoint
    def render_page() -> list[TeamDTO]:
        return executor.render(TeamDTO, rows)

    results = render_page()

    # Assert — exactly one bulk DB call covering both unique ids.
    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})

    assert results[0].owner == User(id=10, name='u10')
    assert results[1].owner == User(id=20, name='u20')
    assert results[2].owner == User(id=10, name='u10')


def test_render_with_function_signature_query() -> None:
    """Same shape, but the prefetch handler is a plain function — no Query[T] subclass."""
    injector = InjectorRegistry()
    queries = QueriesRegistry(injector=injector)  # type: ignore[arg-type]
    transformer = TransformerRegistry(injector=injector)  # type: ignore[arg-type]
    executor = QueryExecutor(queries_registry=queries)  # type: ignore[arg-type]
    injector.bind(QueryExecutor, lambda: executor)

    db_calls: list[frozenset[int]] = []

    @queries
    def fetch_users(ids: frozenset[int]) -> dict[int, User]:
        db_calls.append(ids)
        return {i: User(id=i, name=f'u{i}') for i in ids}

    @transformer(prefetch=fetch_users)
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> User | None:
        users = query_executor.call(fetch_users, ids=batch.ids)
        return users.get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class TeamDTO(BaseModel):
        id: int
        owner: OwnerTransformerAnnotated

    rows: list[dict[str, int]] = [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
    ]

    @injector.entrypoint
    def render_page() -> list[TeamDTO]:
        return executor.render(TeamDTO, rows)

    results = render_page()

    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})
    assert results[0].owner == User(id=10, name='u10')
    assert results[1].owner == User(id=20, name='u20')
