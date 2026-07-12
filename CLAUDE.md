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

`fastbff` is a declarative BFF layer that composes Pydantic response models out of independently registered "queries" and "transformers", with FastAPI-native dependency injection and automatic N+1 avoidance. The big idea: a Pydantic field's `Annotated[...]` metadata declares *how* to populate it; the framework handles batching, caching, and DI.

### Composition root: `FastBFF` + `QueryRouter`

- `QueryRouter` (`fastbff/router.py`) is a pure registry — it collects `@router.queries` and `@router.transformer` callables with their `QueryAnnotation` / `TransformerAnnotation` metadata. No DI wiring.
- `FastBFF` (`fastbff/app.py`) owns a single internal `QueryRouter`, the `query_type → QueryAnnotation` index, and the FastAPI `dependency_overrides` map. `app.include_router(router)` merges a router's registrations into the app and raises `QueryRegistrationError` on duplicates.
- `FastBFF.finalize()` (called implicitly by `mount`) walks every registered handler, dedups their `Annotated[..., Depends(...)]` params, and synthesises a `provide_query_executor(**deps)` factory whose `__signature__` declares those deps as keyword-only parameters. FastAPI's `get_dependant` reads `__signature__`, so the synthetic factory plugs straight into FastAPI's resolver.
- `app.mount(fastapi_app)` copies `dependency_overrides` (including `QueryExecutor → provide_query_executor`) into the user-owned FastAPI app. Endpoints then declare `Annotated[QueryExecutor, Depends(QueryExecutor)]` and FastAPI resolves a fresh executor per request.
- Any registration call invalidates the finalised factory (`_invalidate_finalize`); finalize is idempotent and re-runs only when the handler set changes.

### `QueryExecutor` and the two cache layers

`QueryExecutor` (`fastbff/query_executor/query_executor.py`) is per-request and holds:

1. The shared `query_type → QueryAnnotation` index from the app.
2. A `resolved_deps` dict — the kwargs FastAPI resolved for `provide_query_executor`.
3. A `handler_index[func][arg_name] → synthetic_name | QUERY_EXECUTOR_SENTINEL` mapping.

`fetch(query_obj)` looks up the registered handler, calls `deps_for(handler)` to build its kwargs from the resolved-deps map (substituting `self` wherever `QUERY_EXECUTOR_SENTINEL` appears), then dispatches through `QueryCache`:

- **Call-level cache** for plain return types (key = handler + args).
- **Entity-level cache** for `dict[K, V]`-returning queries that have an `Iterable[K]` field on their `Query` subclass. Overlapping ID sets share cached entries, only missing IDs are fetched from the underlying handler. Absences are remembered, so re-asking does not re-hit the backend.

`QueryExecutor.__init__` is parameterless so that `inspect.signature(QueryExecutor)` is naturally empty: when an endpoint declares `Depends(QueryExecutor)`, FastAPI's `get_dependant` finds no `__init__` params to treat as request params. The override to `provide_query_executor` fires at solve time and supplies the real instance. Build a populated executor (in tests, the factory) via `QueryExecutor.create(query_annotations, ...)`.

#### Async handlers/transformers (the anyio worker-thread bridge)

`async def` handlers and transformers are supported. The wrinkle is that transformers run as **synchronous** Pydantic validators (`with_info_plain_validator_function`), so the transformer-driven N+1 fetch — the place you'd most want async I/O — cannot `await` in place. fastbff bridges using the same primitive Starlette uses for sync endpoints, so it rides FastAPI's existing concurrency model (one thread pool, asyncio **or** trio backend):

- **Sync endpoint (no extra code).** Starlette already runs a `def` endpoint in an anyio worker thread, so it can call `query_executor.fetch(...)` directly and async handlers still bridge — the endpoint looks identical whether handlers are sync or async.
- **Async endpoint.** Use `await query_executor.afetch(query)`, which offloads the synchronous `fetch` machinery to a worker thread via `anyio.to_thread.run_sync` (the same bounded pool Starlette uses).
- Every handler/transformer call goes through `QueryExecutor.call_handler`: sync callables run inline (in the worker thread); for an `async def` callable it submits the coroutine to the host loop with `anyio.from_thread.run(...)`, blocking the *worker* thread — never the loop. `TransformerAnnotation._validate` dispatches through `call_handler` too, so async transformers bridge the same way.
- Two cases can't be bridged and raise `AsyncDispatchError` (never a bare coroutine): a purely synchronous `fetch` reaching an async handler (no worker-thread portal), and an async handler calling sync `fetch` from the loop thread (would self-deadlock — use `await afetch(...)` there). A `RuntimeError` raised by the coroutine *itself* is distinguished from a bridging failure by the `bridged` flag in `call_handler` and re-raised unchanged.
- `QueryCache` is lock-guarded (compute-outside-lock) so concurrent `afetch` (e.g. `asyncio.gather`) on parallel worker threads is safe; the `fetcher` runs outside the lock so bridging can't deadlock against it.

### `validate_batch` + `BatchArg` + `TransformerAnnotation`

`validate_batch(Model, rows, query_executor=...)` (`fastbff/batch.py`) is the two-phase orchestrator for a page of rows:

- **Phase 1 — Plan.** `populate_context_with_batch` walks rows once and collects every unique id per `BatchArg`-aware transformer field into a `{batch_key: set[ids]}` validation context, then injects the `query_executor` into that context.
- **Phase 2 — Merge.** `Model.model_validate(row, context=ctx)` for each row. `TransformerAnnotation.__get_pydantic_core_schema__` registers a `with_info_plain_validator_function` so each transformer runs as a Pydantic validator. The first row's `query_executor.fetch(...)` issues the bulk call; subsequent rows hit the entity-level cache.

`build_transform_annotated(func)` returns `Annotated[ReturnType, transformer_annotation]` so the same transformer can be reused as a field type across multiple Pydantic models. The function is left unchanged and remains directly callable in tests; `transformer_metadata` recovers the `TransformerAnnotation` from either a decorated function or an annotated field alias.

### Query type metadata (`QueryAnnotation`)

`QueryAnnotation` (`fastbff/query_executor/query_annotation.py`) is computed once per `@queries` registration. It detects the `Query[T]` parameter (or accepts an explicit `@queries(SomeQueryType)` form for parameterless handlers), validates the return type matches `Query[T]`, and pre-computes `dict[K, V]` metadata + the IDs field name so `QueryExecutor.fetch` can route into the entity-level cache without re-reflecting at runtime.

### Errors

All errors subclass `FastBFFError` (`fastbff/exceptions.py`). The most common ones surface at registration / include time, not at request time — `QueryRegistrationError` for bad `@queries` declarations and duplicates, `TransformerRegistrationError` for bad `@transformer` declarations, `BatchContextMissingError` when a `BatchArg` transformer is invoked outside `validate_batch`.

## Conventions

- `ruff` is configured with `force-single-line = true` for imports and single-quote `format.quote-style`. Don't reformat into parenthesised import groups.
- `ty` is in `warn` mode for the rules that conflict with Pydantic/FastAPI runtime dynamism (`unresolved-attribute`, `invalid-return-type`, `invalid-type-form`, `invalid-method-override`). Don't tighten those without checking what breaks.
- Python 3.12+ is required (PEP 695 generics are used throughout: `class Foo[T]:`, `def f[F: Callable](...)`).
- The package ships with `py.typed`; runtime deps are `pydantic>=2,<3` and `fastapi>=0.100`.
