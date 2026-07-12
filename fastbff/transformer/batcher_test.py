"""Tests for the batcher — ``populate_context_with_batch`` (Phase 1 "Plan" of
Plan/Fetch/Merge) and ``get_model_batches``, including transformer fields
inherited from a parent model.
"""

from dataclasses import dataclass
from typing import Annotated
from typing import Any

from fastapi import Depends
from pydantic import BaseModel

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import build_transform_annotated
from fastbff.transformer.batcher import get_model_batches
from fastbff.transformer.batcher import populate_context_with_batch


@dataclass(frozen=True)
class _User:
    id: int
    name: str = ''


UserId = int


# ---------------------------------------------------------------------------
# populate_context_with_batch — Phase 1 "Plan"
# ---------------------------------------------------------------------------


def test_populate_context_collects_scalar_ids() -> None:
    router = QueryRouter()

    @router.transformer
    def transform_user(user_id: UserId, batch: BatchArg[UserId]) -> _User:
        return _User(id=user_id, name='')

    UserTransformerAnnotated = build_transform_annotated(transform_user)

    class _Row(BaseModel):
        owner: UserTransformerAnnotated

    rows: list[dict[str, Any]] = [{'owner': 1}, {'owner': 2}, {'owner': 2}]

    context = populate_context_with_batch(_Row, rows)

    batch_key = _Row.__batches__[0].key  # type: ignore[attr-defined]
    assert context == {batch_key: {1, 2}}


def test_populate_context_collects_iterable_ids_and_skips_none() -> None:
    router = QueryRouter()

    @router.transformer
    def transform_user(user_id: UserId, batch: BatchArg[UserId]) -> list[_User]:
        return []

    UsersTransformerAnnotated = build_transform_annotated(transform_user)

    class _Row(BaseModel):
        owners: UsersTransformerAnnotated

    rows: list[dict[str, Any]] = [{'owners': [1, 2, None]}, {'owners': [2, 3]}, {'owners': None}]

    context = populate_context_with_batch(_Row, rows)

    batch_key = _Row.__batches__[0].key  # type: ignore[attr-defined]
    assert context == {batch_key: {1, 2, 3}}


def test_populate_context_returns_empty_for_model_without_batches() -> None:
    class _Row(BaseModel):
        id: int

    context = populate_context_with_batch(_Row, [{'id': 1}, {'id': 2}])
    assert context == {}


# ---------------------------------------------------------------------------
# get_model_batches — transformer fields inherited from a parent model
#
# 1. ``get_type_hints`` merges annotations across the MRO, so a transformer
#    field declared on the parent is discovered on the subclass.
# 2. ``get_model_batches`` must not return the parent's cached ``__batches__``
#    when asked about the subclass (``getattr`` would otherwise walk the MRO).
# ---------------------------------------------------------------------------


def test_inherited_transformer_field_is_discovered_on_subclass() -> None:
    app = FastBFF()

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class _BaseDTO(BaseModel):
        owner: OwnerTransformerAnnotated

    class _TeamDTO(_BaseDTO):
        id: int

    rows = [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    context = populate_context_with_batch(_TeamDTO, rows)

    batches = get_model_batches(_TeamDTO)
    assert len(batches) == 1, 'inherited transformer field should be discovered on _TeamDTO'
    assert batches[0].field_name == 'owner'
    assert context == {batches[0].key: {10, 20}}


def test_subclass_introspection_not_short_circuited_by_parent_cache() -> None:
    """If the parent is introspected first, the subclass must still introspect itself.

    ``getattr(cls, '__batches__', None)`` would otherwise walk the MRO and
    return the parent's cached list — so the subclass's own batches would
    never be computed and any subclass-only transformer field would silently
    drop out of bulk fetching.
    """
    app = FastBFF()

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    @app.transformer
    def transform_admin(
        admin_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(admin_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)
    AdminTransformerAnnotated = build_transform_annotated(transform_admin)

    class _BaseDTO(BaseModel):
        owner: OwnerTransformerAnnotated

    class _TeamDTO(_BaseDTO):
        admin: AdminTransformerAnnotated

    # Trigger parent introspection first — _BaseDTO.__batches__ now lives on
    # the parent class, so _TeamDTO's getattr() lookup would walk MRO and find
    # it without ever introspecting _TeamDTO.
    parent_batches = get_model_batches(_BaseDTO)
    assert len(parent_batches) == 1
    assert parent_batches[0].field_name == 'owner'

    child_batches = get_model_batches(_TeamDTO)
    field_names = {batch.field_name for batch in child_batches}
    assert field_names == {'owner', 'admin'}, (
        f'subclass should see both inherited and own transformer fields, got {field_names}'
    )


def test_inherited_transformer_field_renders_end_to_end() -> None:
    """End-to-end: Plan/Fetch/Merge over a subclass with parent-declared transformer."""
    app = FastBFF()
    db_calls: list[frozenset[int]] = []

    class _FetchUsers(Query[dict[int, _User]]):
        ids: frozenset[int]

    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, _User]:
        db_calls.append(args.ids)
        return {i: _User(id=i) for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> _User | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

    class _BaseDTO(BaseModel):
        owner: OwnerTransformerAnnotated

    class _TeamDTO(_BaseDTO):
        id: int

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    def fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    provide_query_executor = app.finalize()
    query_executor = provide_query_executor()
    results = query_executor.fetch(_FetchTeams())

    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})
    assert [row.owner for row in results] == [_User(id=10), _User(id=20), _User(id=10)]
