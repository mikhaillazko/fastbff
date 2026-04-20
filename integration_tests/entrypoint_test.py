"""In-process Plan → Fetch → Merge flow driven via ``@app.entrypoint``.

Exercises the offline path — no HTTP, no database — to keep the core
composition semantics (``validate_batch`` + entity-level cache) covered
independently of the FastAPI/SQLAlchemy integration test.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import build_transform_annotated
from fastbff import validate_batch


@dataclass(frozen=True)
class User:
    id: int
    name: str


def test_render_issues_one_bulk_call_per_page() -> None:
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

    @app.entrypoint
    def render_page(
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> list[TeamDTO]:
        return validate_batch(TeamDTO, rows, query_executor=query_executor)

    results = render_page()

    # Assert — exactly one bulk DB call covering both unique ids.
    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})

    assert results[0].owner == User(id=10, name='u10')
    assert results[1].owner == User(id=20, name='u20')
    assert results[2].owner == User(id=10, name='u10')
