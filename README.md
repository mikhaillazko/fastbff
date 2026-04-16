# pydantic-bff

Simple back-end for front-end using Pydantic. Declarative data composition with typed
transformers, dependency injection, and automatic N+1 avoidance. Suitable for modular
monolithic systems.

## Features

- **Declarative data composition** — describe the shape of a response once on a Pydantic
  model; fetching happens automatically.
- **One-call orchestration** — `app.executor.render(Model, rows)` runs Plan + Fetch + Merge
  for a whole page in a single line.
- **Typed queries** — `Query[T]` carries its own return type, *or* register a plain
  function with a typed signature; both forms cache identically.
- **Automatic N+1 avoidance** — transformers declare a `BatchArg[T]` and the framework
  plans a single bulk fetch per page instead of one call per row.
- **Two-level cache** — call-level (identical query args) plus entity-level (overlapping
  ID sets are merged into one fetch with only the missing ids).
- **Dependency injection** — built on FastAPI's `Depends`; the same `QueryExecutor` /
  repository / session is shared across every transformer in a request scope.
- **Routers** — register handlers locally on a `QueryRouter` and merge them into a `FastBFF`
  app with `app.include_router(router)`, mirroring FastAPI's `APIRouter`.

## Install

```bash
pip install pydantic-bff
```

Runtime deps: `pydantic>=2`, `fastapi>=0.100`. Python 3.12+ (uses PEP 695 generics).

## Quickstart

```python
from dataclasses import dataclass

from pydantic import BaseModel

from pydantic_bff import (
    FastBFF,
    BatchArg,
    Query,
    QueryExecutor,
    build_transform_annotated,
)

# --- Domain -----------------------------------------------------------------

@dataclass(frozen=True)
class User:
    id: int
    name: str

# --- App --------------------------------------------------------------------

app = FastBFF()

# --- Bulk query -------------------------------------------------------------

class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]

@app.queries
def fetch_users(args: FetchUsers) -> dict[int, User]:
    return {i: User(id=i, name=f'u{i}') for i in args.ids}

# --- Transformer + Response model ------------------------------------------

@app.transformer(prefetch=FetchUsers)
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],
    query_executor: QueryExecutor,
) -> User | None:
    users = query_executor.fetch(FetchUsers(ids=batch.ids))
    return users.get(owner_id)

OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

class TeamDTO(BaseModel):
    id: int
    owner: OwnerTransformerAnnotated

# --- Handler ---------------------------------------------------------------

@app.injector.entrypoint
def render_teams_page() -> list[TeamDTO]:
    rows = [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},  # duplicate id → still just one DB call
    ]
    return app.executor.render(TeamDTO, rows)
```

A single page of N rows issues **one** `fetch_users(...)` call — regardless of N, and
regardless of how many duplicate ids the rows contain.

## Three-phase execution (under the hood)

`app.executor.render(Model, rows)` does three things:

```
Phase 1 — Plan    populate_context_with_batch(Model, rows)
                  → walks rows, collects every unique id for every BatchArg field
                    into {batch_key: set[ids]}

Phase 2 — Fetch   for each @transformer(prefetch=Q): executor.fetch(Q(ids=...))
                  → one bulk @query call per batch, populating the QueryExecutor cache

Phase 3 — Merge   Model.model_validate(row, context=ctx) for each row
                  → each @transformer runs with dependencies injected; every
                    fetch() inside a transformer is a guaranteed cache hit
```

You can run any phase manually if you need to (e.g. mix in custom prefetch logic) —
see `populate_context_with_batch`, `get_model_batches`, and `executor.fetch` /
`executor.call`.

## Core concepts

### `Query[T]` + `@queries`

A `Query[T]` subclass is a typed request object whose return type `T` is recovered
from Pydantic's own generic metadata.

```python
class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]

@app.queries
def fetch_users(args: FetchUsers) -> dict[int, User]:
    ...
```

Return-type mismatches raise `QueryRegistrationError` at registration time, not at
runtime.

### Function-signature queries

If you don't want a `Query[T]` subclass per call, register a plain typed function and
dispatch via `app.executor.call`:

```python
@app.queries
def fetch_users(ids: frozenset[int]) -> dict[int, User]:
    ...

users = app.executor.call(fetch_users, ids=frozenset({1, 2, 3}))
```

The same call-level + entity-level caches apply.

### `QueryExecutor.fetch` / `QueryExecutor.call`

Per-request dispatcher with two caching layers:

- **Call-level** — identical query args return the cached result.
- **Entity-level** — for `dict[K, V]`-returning queries whose request has an `Iterable`
  field, overlapping ID sets are merged. A second call with ids `{2, 3, 4}` after the
  first with `{1, 2, 3}` only fetches `{4}`. Absent ids (returned `{}` from the
  backend) are remembered too, so asking again doesn't hit the backend.

Absence is cached per-executor (per-request). With FastAPI integration (below)
each request gets a fresh `QueryExecutor` automatically.

### `@transformer` + `build_transform_annotated`

A transformer is a plain function with a return type annotation. `@app.transformer`
registers it and returns the function unchanged — directly callable in tests. Use
`build_transform_annotated(func)` to build a Pydantic-ready
`Annotated[ReturnType, TransformerFieldInfo]` alias; bind it to a PascalCase
`<Name>TransformerAnnotated` name and use it directly as a field type:

```python
@app.transformer
def transform_owner(owner_id: int, query_executor: QueryExecutor) -> User | None:
    ...

OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

class TeamDTO(BaseModel):
    owner: OwnerTransformerAnnotated
```

The return type baked into the alias is exactly the function's declared return type
(including `Optional`, `list[...]`, etc.) — reuse the alias on as many models as you
like:

```python
class CommentDTO(BaseModel):
    author: OwnerTransformerAnnotated
```

For unit testing, recover the DI-wrapped underlying callable with
`transformer_callable`:

```python
from pydantic_bff import transformer_callable

call = transformer_callable(transform_owner)
assert call(owner_id=1, query_executor=fake) == User(id=1, name='…')
```

### `BatchArg[T]` + `prefetch=`

Declaring a `BatchArg[T]` parameter on a transformer opts into bulk fetching. Pair it
with `prefetch=` so `executor.render(...)` knows which query to call:

```python
@app.transformer(prefetch=FetchUsers)
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],            # all ids for this field on the current page
    query_executor: QueryExecutor,
) -> User | None:
    users = query_executor.fetch(FetchUsers(ids=batch.ids))
    return users.get(owner_id)
```

If you orchestrate Phase 2 yourself, omit `prefetch=` — the framework will skip the
prefetch step and you can call `executor.fetch(...)` manually before validation.

### `bff_model` (optional)

Pydantic models are auto-introspected on first `populate_context_with_batch` /
`executor.render` call. The `@bff_model` decorator is optional — use it only when you
want introspection paid at import time, or to make the intent visible:

```python
@bff_model
class TeamDTO(BaseModel):
    owner: OwnerTransformerAnnotated
```

### Dependency injection

`InjectorRegistry` wraps FastAPI's `Depends`. Registration decorators (`@app.queries`,
`@app.transformer`) automatically wrap your callables so FastAPI-style dependencies
resolve at call time:

```python
@app.queries
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, User]:
    # `session: DBSession` is Annotated[Session, Depends(get_session)] elsewhere
    ...

@app.injector.entrypoint
def handler() -> ...:
    # `entrypoint` opens a fresh dependency scope for this call
    ...
```

`app.bind(InterfaceOrAnnotatedAlias, factory)` registers a provider — no need to unwrap
`__origin__` for `@dependency`-decorated services. Works equally well with test doubles:

```python
app.bind(QueryExecutor, lambda: shared_executor)
app.bind(SomeService, lambda: FakeService())
```

### `QueryRouter` + `app.include_router`

For multi-module apps, register handlers locally on a `QueryRouter` and attach the
whole bundle to a `FastBFF` app at composition time — exactly like FastAPI's `APIRouter`:

```python
from pydantic_bff import FastBFF, QueryRouter

# users/handlers.py
router = QueryRouter()

@router.queries
def fetch_users(args: FetchUsers) -> dict[int, User]: ...

@router.transformer(prefetch=FetchUsers)
def transform_owner(
    owner_id: int, batch: BatchArg[int], query_executor: QueryExecutor,
) -> User | None: ...

# main.py
app = FastBFF()
app.include_router(router)
```

`include_router` merges the router's queries into the app's registry and rewires the
router's DI plumbing to share the app's. Field annotations built via
`build_transform_annotated` continue to work — no rebuilding required.

Duplicate registrations (same `Query` subclass or same function on both router and app)
raise `QueryRegistrationError` at include time so collisions surface during composition,
not at runtime.

### FastAPI integration

`QueryExecutor` is request-scoped naturally because `@dependency`-decorated services
participate in FastAPI's normal `Depends(...)` lifecycle. A complete route:

```python
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from pydantic_bff import (
    FastBFF, BatchArg, Query, QueryExecutor, build_transform_annotated,
)

# --- SQLAlchemy wiring -----------------------------------------------------

engine = create_engine('postgresql+psycopg://localhost/app')
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_db_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session

DBSession = Annotated[Session, Depends(get_db_session)]

# --- App + route -----------------------------------------------------------

app = FastBFF()
fastapi_app = FastAPI()

class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]

@app.queries
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, User]:
    stmt = select(UserRow).where(UserRow.id.in_(args.ids))
    rows = session.execute(stmt).scalars().all()
    return {row.id: User(id=row.id, name=row.name) for row in rows}

@app.transformer(prefetch=FetchUsers)
def transform_owner(
    owner_id: int, batch: BatchArg[int], query_executor: QueryExecutor,
) -> User | None:
    return query_executor.fetch(FetchUsers(ids=batch.ids)).get(owner_id)

OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

class TeamDTO(BaseModel):
    id: int
    owner: OwnerTransformerAnnotated

@fastapi_app.get('/teams', response_model=list[TeamDTO])
def list_teams(query_executor: QueryExecutor, session: DBSession) -> list[TeamDTO]:
    rows = session.execute(select(TeamRow)).mappings().all()
    return query_executor.render(TeamDTO, rows)
```

The `DBSession` alias is a plain FastAPI `Depends(...)` — pydantic-bff's
`@app.queries` and `@app.transformer` decorators wrap your callable with the
injector, so FastAPI-style `Depends` parameters resolve at call time exactly
as they would in a FastAPI route handler. The same `Session` instance is
reused across every query/transformer in a single request.

`QueryExecutor` is `@dependency`-decorated, so the exported symbol *is*
`Annotated[QueryExecutor, Depends(QueryExecutor)]` — FastAPI walks the
`Annotated` metadata and resolves it as a per-request dependency. No need for
`= Depends()` defaults or extra wrapping at the call site.

Each HTTP request gets a fresh `QueryExecutor` (per-request cache, per-request
absence tracking). Override providers in tests via FastAPI's standard
`fastapi_app.dependency_overrides`, or the injector's own `app.bind(...)`.

### Testing with `QueryExecutorMock`

```python
from pydantic_bff import QueryExecutorMock

mock = QueryExecutorMock(queries_registry=app.queries)
mock.stub_query(FetchUsers, {10: User(id=10, name='u10')})

assert mock.fetch(FetchUsers(ids=frozenset({10}))) == {10: User(id=10, name='u10')}
mock.reset_mock()  # clear stubs; subsequent fetch() calls hit real @queries handlers
```

## Errors

All errors raised by the library subclass `PydanticBFFError`. Common ones:

- `QueryRegistrationError` — bad `@queries` declaration (missing return type, mismatch),
  or duplicate registration when including a router.
- `TransformerRegistrationError` — bad `@transformer` declaration, or
  `build_transform_annotated` called on an unregistered function.
- `QueryNotRegisteredError` — `fetch`/`call` against an unregistered handler.
- `BatchContextMissingError` — transformer with `BatchArg` invoked without context
  (forgot `populate_context_with_batch` or `executor.render`).
- `DependencyResolutionError` — one or more `Depends(...)` parameters failed to resolve.
- `DependencyOverrideError` — `DependenciesSetup.override(...)` targeted an
  unregistered interface.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency management,
[ruff](https://docs.astral.sh/ruff/) for lint + format,
[ty](https://docs.astral.sh/ty/) for type checking, and
[pre-commit](https://pre-commit.com/) to run them on every commit.

```bash
uv sync                         # install project + dev deps into .venv
uv run pytest                   # run the test suite
uv run ruff check . --fix       # lint + autofix
uv run ruff format .            # format
uv run ty check src             # type check
uv run pre-commit install       # install git hooks
uv run pre-commit run --all-files
```

Tests are colocated with the modules they exercise, using the `_test.py` suffix
(e.g. `src/pydantic_bff/query_executor/query_executor_test.py`). The cross-cutting
three-phase integration test lives at `integration_test.py` in the project root.

## License

MIT
