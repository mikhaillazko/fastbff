# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Principles

1. **Simplicity in DX.** Every feature is judged by what the end developer has to write. Prefer to move work into the framework over asking the user to spell it out. Boilerplate that the framework can derive should not exist in user code.
2. **Typing everywhere, no `type: ignore`.** Public surface and user-facing examples must type-check cleanly without ignores or casts. If an idiom requires `# type: ignore`, that is a signal the design is wrong — change the API, not the comment.
3. **Annotation + reflection over duplicated declaration.** When a fact is already encoded in a type annotation (return type, field type, `Annotated[...]` metadata), recover it via `typing.get_type_hints` / `get_args` / `get_origin` instead of asking the user to declare it a second time. The annotation is the source of truth.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for env + deps, [ruff](https://docs.astral.sh/ruff/) for lint/format, and [ty](https://docs.astral.sh/ty/) for type checking.

```bash
uv sync                                  # install project + dev deps into .venv
uv run pytest                            # full suite (unit + integration)
uv run pytest fastbff/router_test.py     # one test module
uv run pytest fastbff/router_test.py::test_name   # one test
uv run ruff check . --fix                # lint + autofix
uv run ruff format .                     # format
uv run ty check fastbff                  # type check (only the package, not tests)
uv run pre-commit run --all-files        # everything CI runs
```

CI matrix runs Python 3.12, 3.13, 3.14. The local pin is in `.python-version`.

## Test layout

`pytest` is configured (`pyproject.toml`) to discover `*_test.py` files under both `fastbff/` and the project root:

- **Unit tests** are colocated with the module they exercise (`fastbff/router.py` → `fastbff/router_test.py`).
- **Integration tests** live in `integration_tests/` and assemble a real FastBFF app on top of FastAPI + SQLAlchemy + SQLite (`integration_tests/sample_app.py` is the shared fixture; tests drive it via `TestClient`).
- Sdist/wheel builds exclude `**/*_test.py` (see `[tool.hatch.build.targets.*]`).

`conftest.py` provides `app`, `query_router`, and `query_executor` fixtures.

## Architecture

`fastbff` is a declarative BFF layer that composes Pydantic response models out of independently registered "queries", with FastAPI-native dependency injection and automatic N+1 avoidance. The big idea: a Pydantic field's `Annotated[..., Resolve(...)]` metadata declares *how* to populate a relation; the framework handles batching, caching, and DI. The executor core is **async-native** (ADR 0002; see `docs/adr/0002-async-core-and-resolve-phase.md`).

### Composition root: `FastBFF` + `QueryRouter`

- `QueryRouter` (`fastbff/router.py`) is a pure registry — it collects `@router.queries` callables with their `QueryAnnotation` metadata. No DI wiring. There is no transformer decorator; composition is declared with `Resolve` fields (below).
- `FastBFF` (`fastbff/app.py`) owns a single internal `QueryRouter`, the `query_type → QueryAnnotation` index, and the FastAPI `dependency_overrides` map. `app.include_router(router)` merges a router's registrations and raises `QueryRegistrationError` on duplicates.
- `FastBFF.finalize()` (called implicitly by `mount`) walks every registered query handler **and every resolver discovered from the queries' response models** (`_discover_resolvers` → `iter_resolves`), dedups their `Annotated[..., Depends(...)]` params, and synthesises a `provide_query_executor(**deps)` factory whose `__signature__` declares those deps as keyword-only parameters. FastAPI's `get_dependant` reads `__signature__`, so the synthetic factory plugs straight into FastAPI's resolver. `_validate_resolve_targets` fails at finalize if a `Resolve(QueryType)` points at a non-`EntityQuery`.
- `app.mount(fastapi_app)` copies `dependency_overrides` into the user-owned FastAPI app: `QueryExecutor → provide_query_executor` (async endpoints) **and** `SyncQueryExecutor → provide_sync_query_executor` (sync endpoints, which wraps the resolved `QueryExecutor`). Endpoints declare `Annotated[QueryExecutor, Depends(QueryExecutor)]` or `Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)]`.
- Any registration call invalidates the finalised factory (`_invalidate_finalize`); finalize is idempotent and re-runs only when the handler set changes.

### `QueryExecutor` and the two cache layers

`QueryExecutor` (`fastbff/query_executor/query_executor.py`) is per-request, **async-native**, and holds:

1. The shared `query_type → QueryAnnotation` index from the app.
2. A `resolved_deps` dict — the kwargs FastAPI resolved for `provide_query_executor`.
3. A `handler_index[func][arg_name] → synthetic_name | QUERY_EXECUTOR_SENTINEL` mapping.

`async def fetch(query_obj)` looks up the registered handler, calls `deps_for(handler)` to build its kwargs (substituting `self` wherever `QUERY_EXECUTOR_SENTINEL` appears), invokes the handler via `_call` (async handlers awaited on the loop; sync handlers offloaded to one anyio worker thread via `anyio.to_thread.run_sync`), then dispatches through `QueryCache`:

- **Call-level cache** for plain `Query[T]` return types (key = handler + args). Concurrent identical fetches share one in-flight `asyncio.Future` (`get_or_call`), so a backend call runs at most once per key.
- **Entity-level cache** for `EntityQuery[K, V]` — a `Query[dict[K, V]]` subclass with an ids field. Overlapping ID sets share cached entries, only missing IDs are fetched; absences are remembered. Entity caching is **explicit opt-in** (an `EntityQuery` subclass), never inferred from a `dict` return shape.

When a query's result model (`Query[T].T`) declares `Resolve` fields, `fetch` runs the render pipeline (`annotation.render_target` → `apply_render`) after the handler returns.

`QueryExecutor.__init__` is parameterless so `inspect.signature(QueryExecutor)` is naturally empty: `Depends(QueryExecutor)` presents no request params, and the mount-time override supplies the real instance. Build a populated executor via `QueryExecutor.create(query_annotations, ...)`.

#### Async model (loop-native fetch, sync facade)

`fetch` is a coroutine and runs on the event loop:

- **Async endpoint.** `await query_executor.fetch(query)`. Async handlers/resolvers are awaited directly on the loop (no worker thread), so async composition (a resolver that `await`s `fetch` of another query) never consumes a worker thread per level or deadlocks the bounded pool.
- **Sync endpoint.** Inject `SyncQueryExecutor` and call `sync_query_executor.fetch(query)`. Starlette runs the sync endpoint in an anyio worker thread; `SyncQueryExecutor.fetch` bridges onto the loop via `anyio.from_thread.run`, then the async core takes over. Cost: one loop round-trip per top-level `fetch`.
- `QueryExecutor._call` bridges the two thread contexts: `async def` callables are awaited on the loop; plain `def` callables are offloaded to a single anyio worker thread via `anyio.to_thread.run_sync`, preserving thread affinity for whatever request-scoped resource (e.g. a DB session) they touch.
- `QueryCache` is loop-native — every method is a coroutine and all mutations happen between `await` points, so no thread lock is needed. Call-level dedup uses a per-key `asyncio.Future`; entity fetches serialise per bucket with an `asyncio.Lock` (held across the awaited fetch — safe, cooperative, not a thread lock).

### The `Resolve` pipeline (`fastbff/resolve.py`)

`Resolve` is inert Pydantic metadata (no core schema) placed in `Annotated[T, Resolve(...)]`. Two forms: `Resolve(SomeEntityQuery)` (the raw row value is a key into the entity query's `dict[K, V]`) or `Resolve(resolver=fn)` (a batch-first `(ids, *deps) -> dict[key, value]` callable; the executor is injected by type, `Depends(...)` params via `handler_index`). `render(model, rows, executor)` is the three-phase orchestrator for a page of rows:

- **Phase 1 — Plan.** Walk rows once, collecting the id-set per `Resolve` field (`get_resolve_fields`) and any nested resolve-bearing model fields (`get_nested_fields`), both cached on the model class.
- **Phase 2 — Fetch.** One bulk call per field, **all fields concurrent** via `asyncio.gather` (`_render_resolve` calls `executor.resolve_ids`; nested fields re-enter `render` as their own batch — depth-first, one bulk fetch per field per level).
- **Phase 3 — Merge.** Substitute resolved values into each row, then `model_validate(row)` — context-free and side-effect-free, so DTOs validate anywhere.

`classify_render(Query[T].T)` decides `('list'|'single', Model) | None` (cached lazily on `QueryAnnotation.render_target`, since a forward-referenced model may not be built when the decorator fires).

### Query type metadata (`QueryAnnotation`)

`QueryAnnotation` (`fastbff/query_executor/query_annotation.py`) is computed once per `@queries` registration. It detects the `Query[T]` parameter (or accepts an explicit `@queries(SomeQueryType)` form for parameterless handlers), validates the return type, and — for `EntityQuery` subclasses — pre-computes the key/value types and the ids field name (`_find_ids_field`, preferring a field named `ids`) so `fetch` routes into the entity cache without re-reflecting at runtime.

### Errors

All errors subclass `FastBFFError` (`fastbff/exceptions.py`). The most common ones surface at registration / finalize time, not at request time — `QueryRegistrationError` for bad `@queries` declarations, duplicates, and an `EntityQuery` with no discoverable ids field; `ResolveRegistrationError` for a mis-declared `Resolve` field (neither/both args, or a `Resolve(QueryType)` targeting a non-`EntityQuery`). `QueryNotRegisteredError` and `CacheKeyError` surface at request time.

## Conventions

- `ruff` is configured with `force-single-line = true` for imports and single-quote `format.quote-style`. Don't reformat into parenthesised import groups.
- `ty` is in `warn` mode for the rules that conflict with Pydantic/FastAPI runtime dynamism (`unresolved-attribute`, `invalid-return-type`, `invalid-type-form`, `invalid-method-override`). Don't tighten those without checking what breaks.
- Python 3.12+ is required (PEP 695 generics are used throughout: `class Foo[T]:`, `def f[F: Callable](...)`).
- The package ships with `py.typed`; runtime deps are `pydantic>=2,<3` and `fastapi>=0.100`.
