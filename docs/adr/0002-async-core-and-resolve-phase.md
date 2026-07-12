# ADR 0002 — Async-native executor core and explicit resolve phase

| Field      | Value                                                                              |
|------------|------------------------------------------------------------------------------------|
| Status     | Accepted                                                                           |
| Deciders   | maintainers                                                                        |
| Date       | 2026-07-13                                                                         |
| Supersedes | —                                                                                  |
| Related    | ADR 0001 (DI rework) — Option D interacts with Decision 1; `TODO.md` P0 #1, P0 #2  |

## Context

Two structural pressures have accumulated:

### P1 — Duplicated sync/async execution paths

Async support (TODO P0 #1) was added as a parallel path rather than a core:
`QueryExecutor.fetch` / `afetch` / `_afetch_async`, and `QueryCache.get_or_call` /
`aget_or_call`, `get_or_fetch_entities` / `aget_or_fetch_entities` are near-copies
(~150 duplicated lines across `query_executor.py` and `query_cache.py`). Every
behavioral change must be applied twice; divergence is a standing bug risk.
The bridging machinery (`call_handler`, `AsyncDispatchError`, the
`iscoroutinefunction`-slipped-past guard) exists only to reconcile the two paths.

Performance consequences of the current shape:

- A sync handler subtree under `afetch` occupies a full anyio worker thread.
- Transformer-driven bulk fetches bridge onto the loop **one at a time**
  (acknowledged in TODO P0 #1 follow-up) — independent `Resolve`d fields on the
  same page are fetched sequentially.

### P2 — I/O executes inside Pydantic validation

Transformers run as `with_info_plain_validator_function` validators during
`model_validate`, receiving the `QueryExecutor` via `ValidationInfo.context`
(a service-locator hand-off). Consequences:

| Symptom | Root cause |
| --- | --- |
| `BatchContextMissingError` when a model is validated outside a fastbff dispatch boundary | field values are not values — they require a live executor |
| Fetch failures surface as `ValidationError` | I/O and validation share one phase |
| Async transformers need the worker-thread offload in `_afetch_async` | Pydantic validators cannot `await` |
| DTOs are not testable as plain models | same as above |
| Three user-facing concepts per relation (`@transformer`, `BatchArg`, `build_transform_annotated`) | batching is expressed through the validator calling convention |

P2 is the *cause* of the worst part of P1: the sync-validator constraint is what
forces the thread-offload special case in the async path.

## Proposed decisions

### Decision 1 — Single async-native core; sync becomes a facade

Rewrite `QueryExecutor` and `QueryCache` with **one async implementation**.

```python
class QueryExecutor:
    async def fetch[T](self, query_obj: Query[T]) -> T: ...

    async def _call(self, handler, kwargs):
        if iscoroutinefunction(handler):
            return await handler(**kwargs)                       # loop-native
        return await anyio.to_thread.run_sync(partial(handler, **kwargs))

class SyncQueryExecutor:
    """For sync endpoints (already on an anyio worker thread)."""
    def fetch[T](self, query_obj: Query[T]) -> T:
        return anyio.from_thread.run(self._inner.fetch, query_obj)
```

- `QueryCache` keeps a single async implementation. Concurrent identical fetches
  deduplicate on a per-key `asyncio.Future` (awaiters share one in-flight fetch)
  instead of today's "both run, first `setdefault` wins".
- `call_handler`, `AsyncDispatchError`, `aget_or_call` / `aget_or_fetch_entities`
  twins, and the coroutine-slipped-past guard are deleted.
- Sync handlers remain fully supported (offloaded via `to_thread.run_sync`);
  thread affinity for a request-scoped `Session` is preserved by confining a
  sync subtree to one worker, as today.

**Alternatives considered**

- **(a) unasync codegen** (urllib3/psycopg style): write async, generate sync at
  build time. Rejected — adds build tooling and a generated-code review burden
  disproportionate to a ~2 kLOC library.
- **(b) sans-IO core** (h11 style): pure planning state machine, IO drivers
  outside. Cleanest in theory; rejected as over-engineering — fastbff's "IO" is
  a single `fetch` seam, not a protocol.
- **(c) keep dual paths, add lint discipline**: rejected — does not remove the
  duplication, and blocks Decision 2 (which needs `await` in the composition
  path).

### Decision 2 — Replace validator-driven transformers with an explicit resolve phase

Composition moves out of Pydantic validation into a three-phase pipeline owned
by the executor (the DataLoader / GraphQL-executor separation):

```
Phase 1  PLAN     walk raw rows, collect id-set per Resolve field   (pure)
Phase 2  FETCH    one bulk query per field, all fields concurrent   (I/O)
                  results = await asyncio.gather(*per_field_fetches)
Phase 3  MERGE    substitute values into rows, then
                  model_validate(row)  — context-free, side-effect-free
```

User-facing API: the `@transformer` + `BatchArg` + `build_transform_annotated`
triple is replaced by one field annotation.

```python
class FetchUsers(EntityQuery[int, User]):        # see Decision 3
    ids: frozenset[int]

class TeamDTO(BaseModel):
    id: int
    owner: Annotated[User | None, Resolve(FetchUsers)]          # key = raw row["owner"]
    project: Annotated[Project | None, Resolve(FetchProjects)]
```

Custom logic is a **resolver**: a plain (async) function with the batch-first
signature `(ids, deps...) -> dict[key, value]`, executed in Phase 2:

```python
async def resolve_owner(ids: frozenset[int], ex: QueryExecutor) -> dict[int, User]:
    users = await ex.fetch(FetchUsers(ids=ids))
    return {i: u for i, u in users.items() if u.active}

owner: Annotated[User | None, Resolve(resolver=resolve_owner)]
```

Nested models resolve depth-first: Phase 3 output rows that themselves contain
`Resolve` fields re-enter Phase 1 per nesting level (bounded by model depth;
one bulk fetch per field per level — the N+1 guarantee is preserved recursively).

Properties gained:

- `Model.model_validate(...)` works anywhere; `BatchContextMissingError` is deleted.
- Fetch errors are raised from Phase 2 as fetch errors, attributable to a field.
- Resolvers are natively async; independent fields fetch concurrently
  (`asyncio.gather`), closing the TODO P0 #1 follow-up.
- DI into resolvers goes through the same handler-deps mechanism as `@queries`
  — no executor smuggled through `ValidationInfo.context`.

**Alternatives considered**

- **(a) keep validator-driven design, add async validators when Pydantic supports
  them**: rejected — timeline outside our control, and does not fix error
  conflation, context coupling, or standalone `model_validate`.
- **(b) computed fields / lazy proxies** (resolve on attribute access): rejected —
  hides I/O behind attribute reads, breaks serialization determinism, defeats
  page-level batching.
- **(c) GraphQL adoption (strawberry + DataLoader)**: solves the same problem but
  forces a protocol change; out of scope for a REST BFF library. The *executor
  architecture* is deliberately borrowed from it.

### Decision 3 — Entity-level caching becomes explicit opt-in

Today entity caching triggers structurally: a handler returning `dict[K, V]`
whose query has *any* iterable field. Adding an unrelated iterable field
(e.g. `statuses: set[str]`) silently changes cache semantics.

```python
class FetchUsers(EntityQuery[int, User]):   # declares key/value types AND the ids field
    ids: frozenset[int]
```

Plain `Query[T]` keeps call-level caching only. Shape-based detection is removed.

## Consequences

### Positive

- `query_executor.py` (279) + `query_cache.py` (135) + `batch.py` (83) + most of
  `transformer/types.py` (183) collapse to roughly half the code with strictly
  more capability (field-level concurrency, in-flight dedup).
- One concept (`Resolve`) replaces three; per-relation boilerplate drops from
  ~15 lines to 1.
- DTOs, resolvers, and the pipeline (`render(model, rows)`) are each unit-testable
  in isolation.
- Observability seam appears naturally: Phase 2 is the single place to emit
  planned-batch / cache-hit metrics (OpenTelemetry), making the N+1 claim
  verifiable in production.

### Negative / risks

- **Breaking change** to the public API (`@transformer`, `BatchArg`,
  `build_transform_annotated`, `afetch` all removed). Acceptable at 0.x /
  Development Status :: Beta; ship as **0.3.0** with a migration guide mapping
  each old construct to its replacement.
- Nested resolution requires recursive planning (`plan_for` walks sub-models);
  more executor complexity than today's flat context — mitigated by per-model
  plan caching (same lazy-introspection approach as `get_model_batches`).
- Sync endpoints pay one `from_thread.run` hop per `fetch`. Measured cost is
  microseconds against a DB round-trip; documented, not optimized.
- The "it's just Pydantic" elegance is lost: validation no longer *drives*
  composition. This is judged the point, not a regression.

### Interaction with ADR 0001

Decision 2 makes ADR 0001 Option D (per-endpoint dep scoping) cheaper:
`Resolve` fields are statically discoverable from the endpoint's response model,
so the per-endpoint dependency closure can be computed at finalize time without
runtime tracing.

## Migration sketch (0.2 → 0.3)

| 0.2 construct | 0.3 replacement |
| --- | --- |
| `@transformer` + `BatchArg` + `build_transform_annotated` | `Annotated[T, Resolve(QueryType)]` or `Resolve(resolver=fn)` |
| `query_executor.fetch(...)` (sync endpoint) | `SyncQueryExecutor.fetch(...)` |
| `await query_executor.afetch(...)` | `await query_executor.fetch(...)` |
| `dict[K, V]` query + iterable field (implicit entity cache) | `EntityQuery[K, V]` |
| `BatchContextMissingError` handling | delete — cannot occur |
| `QueryExecutorMock` (fall-through default) | `QueryExecutorMock(strict=True)` default; `strict=False` opt-in |

## Rollout

1. Land the async-native `QueryCache` with future-based dedup (isolated, testable).
2. Land `QueryExecutor` async core + `SyncQueryExecutor`; port `async_dispatch_test.py`
   scenarios (worker-pool exhaustion regression, concurrent-fetch cache safety).
3. Implement `Resolve` / `plan_for` / three-phase `render`; port integration tests.
4. Delete legacy transformer machinery; write migration guide; release 0.3.0.

## Decision

Accepted and implemented in 0.3.0. All three decisions shipped: the async-native
executor core with a `SyncQueryExecutor` facade, the `Resolve` field annotation
with the plan/fetch/merge render pipeline, and explicit `EntityQuery` opt-in
caching. The legacy transformer machinery (`@transformer`, `BatchArg`,
`build_transform_annotated`, `validate_batch`, `afetch`) was removed. See
`docs/migration/0.2-to-0.3.md`.
