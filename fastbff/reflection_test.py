"""Reflection under PEP 563 — string-annotation resolution end-to-end.

Regression tests for ``from __future__ import annotations``: when a user
enables PEP 563 in the module declaring transformers, queries, or models,
every annotation arrives as a *string* via ``inspect.signature(...)`` —
``param.annotation`` is ``'BatchArg[int]'`` rather than the class. The
reflection layer must resolve those strings (via ``typing.get_type_hints``) so:

* batch fields on the model are still discovered (``populate_context_with_batch``),
* ``Depends(...)`` parameters on transformers/queries are still picked up by
  the synthesised ``provide_query_executor`` factory.

Targets are declared at module level — same as a real app — so PEP 563
string annotations resolve through the module's globals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import build_transform_annotated
from fastbff.transformer.batcher import populate_context_with_batch


@dataclass(frozen=True)
class _User:
    id: int
    name: str


_router = QueryRouter()


@_router.transformer
def _transform_owner(
    owner_id: int,
    batch: BatchArg[int],
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> _User | None:
    return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)


_OwnerTransformerAnnotated = build_transform_annotated(_transform_owner)


class _FetchUsers(Query[dict[int, _User]]):
    ids: frozenset[int]


_db_calls: list[frozenset[int]] = []


@_router.queries
def _fetch_users(args: _FetchUsers) -> dict[int, _User]:
    _db_calls.append(args.ids)
    return {i: _User(id=i, name=f'u{i}') for i in args.ids}


class _TeamDTO(BaseModel):
    id: int
    owner: _OwnerTransformerAnnotated


def test_populate_context_resolves_pep563_batch_field() -> None:
    """Batch fields declared in a PEP 563 module must still be discovered."""
    rows = [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]
    context = populate_context_with_batch(_TeamDTO, rows)

    batch_key = _TeamDTO.__batches__[0].key  # type: ignore[attr-defined]
    assert context == {batch_key: {10, 20}}


def test_render_pep563_module_issues_one_bulk_call() -> None:
    """Full Plan/Fetch/Merge through a PEP 563-declared transformer + query handler."""
    _db_calls.clear()
    app = FastBFF()
    app.include_router(_router)

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    def _fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    provide_query_executor = app.finalize()
    query_executor = provide_query_executor()
    results = query_executor.fetch(_FetchTeams())

    assert len(_db_calls) == 1
    assert _db_calls[0] == frozenset({10, 20})
    assert [row.owner for row in results] == [
        _User(id=10, name='u10'),
        _User(id=20, name='u20'),
        _User(id=10, name='u10'),
    ]
