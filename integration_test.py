"""End-to-end Plan → Fetch → Merge flow wired through real DI."""

from dataclasses import dataclass

from pydantic import BaseModel

from src.injections.registry import InjectorRegistry
from src.query_executor.query import Query
from src.query_executor.query_executor import QueryExecutor
from src.query_executor.registry import QueriesRegistry
from src.transformer.batcher import populate_context_with_batch
from src.transformer.builder import build_transform_annotated
from src.transformer.decorators import bff_model
from src.transformer.registry import TransformerRegistry
from src.transformer.types import BatchArg


@dataclass(frozen=True)
class User:
    id: int
    name: str


def test_three_phase_flow_issues_one_bulk_call_per_page() -> None:
    # Arrange — wire a real DI container, a queries registry, and a transformer registry.
    injector = InjectorRegistry()
    queries = QueriesRegistry(injector=injector)  # type: ignore[arg-type]
    executor = QueryExecutor(queries_registry=queries)  # type: ignore[arg-type]
    transformer = TransformerRegistry(injector=injector)  # type: ignore[arg-type]

    # Override so that any `Depends(QueryExecutor)` within the scope yields our shared executor.
    qe_class = QueryExecutor.__origin__  # unwrap @dependency Annotated alias
    injector.dependency_provider.dependency_overrides[qe_class] = lambda: executor

    # Bulk @query handler (this is what a real app would back with a DB call).
    db_calls: list[frozenset[int]] = []

    class FetchUsers(Query[dict[int, User]]):
        ids: frozenset[int]

    @queries
    def fetch_users(args: FetchUsers) -> dict[int, User]:
        db_calls.append(args.ids)
        return {i: User(id=i, name=f'u{i}') for i in args.ids}

    # Transformer: takes the per-row id, the batch of all ids for this field, and the injected executor.
    @transformer
    def transform_owner(owner_id: int, batch: BatchArg[int], ex: QueryExecutor) -> User | None:
        users = ex.fetch(FetchUsers(ids=batch.ids))
        return users.get(owner_id)

    OwnerT = build_transform_annotated(transform_owner)

    @bff_model
    class TeamDTO(BaseModel):
        id: int
        owner: OwnerT

    # Raw page of backend rows (e.g. SQL result rows mapped to dicts).
    rows: list[dict[str, int]] = [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},  # duplicate id → should not trigger a second DB call
    ]

    # Act — enter a DI scope and run the three phases.
    @injector.entrypoint
    def render_page() -> list[TeamDTO]:
        # Phase 1 — Plan: collect ids for every batchable field.
        ctx = populate_context_with_batch(TeamDTO, rows)
        # Phase 2 — Fetch: pre-warm the cache with one bulk call per batch.
        for batch_info in TeamDTO.__batches__:  # type: ignore[attr-defined]
            ex = executor  # in real apps, resolved via DI within the same scope
            ex.fetch(FetchUsers(ids=frozenset(ctx[batch_info.key])))
        # Phase 3 — Merge: Pydantic validation fires each transformer; cache hit is guaranteed.
        return [TeamDTO.model_validate(row, context=ctx) for row in rows]

    results = render_page()

    # Assert — exactly one bulk DB call covering both unique ids.
    assert len(db_calls) == 1
    assert db_calls[0] == frozenset({10, 20})

    # Assert — transformed owners are correct.
    assert results[0].owner == User(id=10, name='u10')
    assert results[1].owner == User(id=20, name='u20')
    assert results[2].owner == User(id=10, name='u10')
