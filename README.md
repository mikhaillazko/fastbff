# fastbff

[![CI](https://github.com/mikhaillazko/fastbff/actions/workflows/ci.yml/badge.svg)](https://github.com/mikhaillazko/fastbff/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/mikhaillazko/fastbff/graph/badge.svg)](https://codecov.io/gh/mikhaillazko/fastbff)
[![PyPI](https://img.shields.io/pypi/v/fastbff.svg)](https://pypi.org/project/fastbff/)
[![Python versions](https://img.shields.io/pypi/pyversions/fastbff.svg)](https://pypi.org/project/fastbff/)
[![License: MIT](https://img.shields.io/pypi/l/fastbff.svg)](https://github.com/mikhaillazko/fastbff/blob/master/LICENSE)

Simple back-end for front-end using Pydantic. Declarative data composition with typed
queries, a resolve pipeline, dependency injection, and automatic N+1 avoidance. Suitable
for modular monolithic systems.

## Features

- **Declarative data composition** — describe the shape of a response once on a Pydantic
  model; fetching happens automatically.
- **Zero orchestration boilerplate** — `@queries` handlers return raw rows; the framework
  runs Plan + Fetch + Merge at the dispatch boundary.
- **Typed queries** — `Query[T]` carries its own return type, *or* register a plain
  function with a typed signature; both forms cache identically.
- **Automatic N+1 avoidance** — `Resolve` fields declare how a relation is populated and
  the framework plans a single bulk fetch per relation field per page instead of one call
  per row.
- **Two-level cache** — call-level (identical query args) plus entity-level (`EntityQuery`:
  overlapping ID sets are merged into one fetch with only the missing ids).
- **Async-native executor** — `await query_executor.fetch(...)` on async endpoints; a
  `SyncQueryExecutor` facade bridges onto the event loop for sync endpoints.
- **Dependency injection** — built on FastAPI's `Depends`; the same `QueryExecutor` /
  repository / session is shared across every handler and resolver in a request scope.
- **Routers** — register handlers locally on a `QueryRouter` and merge them into a `FastBFF`
  app with `app.include_router(router)`, mirroring FastAPI's `APIRouter`.

> Upgrading from 0.2? See [`docs/migration/0.2-to-0.3.md`](docs/migration/0.2-to-0.3.md)
> for the exact old→new mapping. `@transformer` / `BatchArg` / `build_transform_annotated`
> are replaced by `Resolve`; `afetch` is gone (`fetch` is now a coroutine).

## Install

```bash
pip install fastbff
```

Runtime deps: `pydantic>=2`, `fastapi>=0.100`. Python 3.12+ (uses PEP 695 generics).

## Quickstart

```python
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from fastbff import (
    EntityQuery,
    FastBFF,
    Query,
    QueryExecutor,
    QueryRouter,
    Resolve,
    SyncQueryExecutor,
)

# --- Domain -----------------------------------------------------------------

@dataclass(frozen=True)
class User:
    id: int
    name: str

# --- Router -----------------------------------------------------------------

router = QueryRouter()

# --- Entity query -----------------------------------------------------------
# `EntityQuery[K, V]` opts into entity-level caching: overlapping id sets share
# cached entries and only the missing ids are fetched.

class FetchUsers(EntityQuery[int, User]):
    ids: frozenset[int]

@router.queries
def fetch_users(query: FetchUsers) -> dict[int, User]:
    return {i: User(id=i, name=f'u{i}') for i in query.ids}

# --- Response model with a Resolve field -----------------------------------
# The raw row value for `owner` (an id) is the key into FetchUsers' dict[int, User]
# result. The render pipeline collects every owner id across the page and issues a
# single bulk fetch.

class TeamDTO(BaseModel):
    id: int
    owner: Annotated[User | None, Resolve(FetchUsers)]

# --- Page-rendering query --------------------------------------------------
# `Query[list[TeamDTO]]` is the output contract; the handler returns honest rows
# (`list[dict]`) and the framework resolves + validates them to TeamDTO at the
# dispatch boundary, planning a single bulk `fetch_users` call for the whole page.

class FetchTeams(Query[list[TeamDTO]]):
    pass

@router.queries(FetchTeams)
def fetch_teams() -> list[dict]:
    return [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},  # duplicate id → still just one DB call
    ]

# --- HTTP route -------------------------------------------------------------

fastapi_app = FastAPI()


@fastapi_app.get('/teams')
def list_teams(
    sync_query_executor: Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)],
) -> list[TeamDTO]:
    return sync_query_executor.fetch(FetchTeams())


@fastapi_app.get('/teams-async')
async def list_teams_async(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    return await query_executor.fetch(FetchTeams())

# --- Compose ----------------------------------------------------------------

app = FastBFF()
app.include_router(router)
app.mount(fastapi_app)
```

A single page of N rows issues **one** `fetch_users(...)` call — regardless of N, and
regardless of how many duplicate ids the rows contain. The handler honestly types its
return as `list[dict]`; `Query[list[TeamDTO]]` is the *output* contract that the executor
honors after running the resolve pipeline.

## The resolve pipeline (under the hood)

When a `@queries` handler is registered with `Query[list[Model]]` (or `Query[Model]`)
where the model has `Resolve` fields, fastbff runs a three-phase render automatically
inside `query_executor.fetch(...)`:

```
Phase 1 — Plan    walk the page of rows once and collect the id-set per Resolve field

Phase 2 — Fetch   one bulk call per field, all fields fetched concurrently
                  (asyncio.gather); entity queries collapse overlapping ids

Phase 3 — Merge   substitute the resolved values into each row, then
                  Model.model_validate(row) — context-free, side-effect-free
```

One bulk query per relation field is the N+1 guarantee. Nested resolve-bearing models
resolve depth-first — a field typed as another `Resolve`-bearing model re-enters the
pipeline as its own batch, so the one-bulk-fetch-per-field guarantee holds recursively.

Handlers that already build model instances directly (e.g. `dict[int, User]` queries
constructing `User(...)` per row) flow through unchanged — already-validated values are
detected and the render is a no-op.

## Core concepts

### `Query[T]` + `@queries`

A `Query[T]` subclass is a typed request object whose return type `T` is recovered
from Pydantic's own generic metadata.

```python
class FetchUser(Query[User]):
    user_id: int

@app.queries
def fetch_user(query: FetchUser) -> User:
    ...
```

Parameterless queries pass the request type to the decorator:

```python
class FetchAll(Query[list[User]]):
    pass

@app.queries(FetchAll)
def fetch_all() -> list[User]:
    ...
```

Return-type mismatches raise `QueryRegistrationError` at registration time, not at
runtime.

### `EntityQuery[K, V]`

`EntityQuery[K, V]` is the opt-in form for entity-level caching. Subclass it with an
`ids` field and return a `dict[K, V]`:

```python
class FetchUsers(EntityQuery[int, User]):
    ids: frozenset[int]

@app.queries
def fetch_users(query: FetchUsers) -> dict[int, User]:
    ...
```

Overlapping ID sets are merged: a second call with ids `{2, 3, 4}` after the first with
`{1, 2, 3}` only fetches `{4}`. Absent ids (missing from the returned dict) are remembered
too, so asking again doesn't hit the backend. A plain `Query[dict[K, V]]` still works but
gets **call-level** caching only — so adding an unrelated iterable field can never silently
change cache semantics.

### `QueryExecutor.fetch` (async) and `SyncQueryExecutor`

`QueryExecutor` is a per-request dispatcher with two caching layers:

- **Call-level** — identical query args return the cached result.
- **Entity-level** — for `EntityQuery` requests, overlapping ID sets are merged and only
  the missing ids are fetched.

Absence is cached per-executor (per-request). With FastAPI integration (below) each
request gets a fresh executor automatically.

`QueryExecutor.fetch` is a **coroutine** — `await` it on async endpoints:

```python
@fastapi_app.get('/teams')
async def list_teams(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    return await query_executor.fetch(FetchTeams())
```

Sync endpoints inject `SyncQueryExecutor` instead and call `.fetch(...)` directly.
Starlette runs a `def` endpoint in a worker thread; `SyncQueryExecutor` bridges onto the
event loop from there, so async handlers still work from a sync endpoint:

```python
@fastapi_app.get('/teams')
def list_teams(
    sync_query_executor: Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)],
) -> list[TeamDTO]:
    return sync_query_executor.fetch(FetchTeams())
```

### `Resolve`

A `Resolve` annotation declares how a relation field is populated. It is inert Pydantic
metadata — it carries no core schema, so a model with `Resolve` fields validates as a
plain model once the resolved values are substituted in. Resolvers are discovered
automatically from the response models your queries return — there is no decorator to add.

**Query form** — `Resolve(SomeEntityQuery)`. The raw row value for the field is a key (or
an iterable of keys) into the `EntityQuery`'s `dict[K, V]` result:

```python
class TeamDTO(BaseModel):
    id: int
    owner: Annotated[User | None, Resolve(FetchUsers)]   # key = raw row['owner']
```

**Resolver form** — `Resolve(resolver=fn)`, for custom logic (filtering, deriving keys, or
calling something other than an `EntityQuery`). `fn` is a batch-first callable
`(ids, *deps) -> dict[key, value]`, sync or `async def`. It receives the collected id-set
plus a `QueryExecutor` injected by **type** (`executor: QueryExecutor`, no `Depends`); any
`Annotated[Dep, Depends(...)]` params are injected exactly as a handler's are:

```python
async def resolve_owner(ids: frozenset[int], executor: QueryExecutor) -> dict[int, User]:
    users = await executor.fetch(FetchUsers(ids=ids))
    return {i: u for i, u in users.items() if u.active}

class TeamDTO(BaseModel):
    id: int
    owner: Annotated[User | None, Resolve(resolver=resolve_owner)]
```

A field annotated as a collection (`list[User]`) whose raw value is an iterable of keys is
resolved element-wise from the same bulk fetch. Reuse a `Resolve` field annotation on as
many models as you like.

### Dependency injection

fastbff defers to FastAPI's own DI: every registered handler is left as-is,
and at finalize time the app synthesises a single `provide_query_executor`
factory whose signature declares the union of every handler's and resolver's
`Annotated[..., Depends(...)]` parameters. FastAPI resolves that graph
once per request and the executor hands the resolved values to each
handler / resolver at dispatch time.

```python
@app.queries
def fetch_users(query: FetchUsers, session: DBSession) -> dict[int, User]:
    # `session: DBSession` is Annotated[Session, Depends(get_session)] elsewhere
    ...
```

`FastBFF` is a `dependency_overrides_provider` — its
`dependency_overrides` dict is the same one FastAPI uses. The
`app.bind(target, factory)` helper is a thin wrapper that writes into it
and accepts both a bare class and its `Annotated[Class, Depends(Class)]`
alias, mapping both to the same override key:

```python
app.bind(QueryExecutor, lambda: shared_executor)
app.bind(SomeService, lambda: FakeService())
```

Bind *before* `app.mount(fastapi_app)` — `mount` copies overrides into
the FastAPI app's `dependency_overrides` once.

### Module organisation

Declare your queries, resolvers, and DTOs at module scope. fastbff
introspects them with `typing.get_type_hints`, which resolves string
annotations against the *module's* globals — so models declared in
modules with `from __future__ import annotations` (PEP 563) work out of
the box, but a class or function defined inside another function and
referencing other locals will fail to resolve. This is the same
constraint Pydantic itself imposes.

### `QueryRouter` + `app.include_router`

For multi-module apps, register handlers locally on a `QueryRouter` and attach the
whole bundle to a `FastBFF` app at composition time — exactly like FastAPI's `APIRouter`:

```python
from fastbff import FastBFF, QueryRouter

# users/handlers.py
router = QueryRouter()

@router.queries
def fetch_users(query: FetchUsers) -> dict[int, User]: ...

# main.py
app = FastBFF()
app.include_router(router)
```

`include_router` merges the router's queries into the app's registry and rewires the
router's DI plumbing to share the app's. `Resolve` fields on response models continue to
work — no rebuilding required.

Duplicate registrations (same `Query` subclass or same function on both router and app)
raise `QueryRegistrationError` at include time so collisions surface during composition,
not at runtime.

### FastAPI integration

`QueryExecutor` / `SyncQueryExecutor` are request-scoped naturally: annotate an endpoint
parameter as `Annotated[QueryExecutor, Depends(QueryExecutor)]` (async) or
`Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)]` (sync) and FastAPI's own
`Depends(...)` pipeline resolves a fresh instance per request. A complete route:

```python
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fastbff import EntityQuery, FastBFF, Query, QueryExecutor, Resolve, SyncQueryExecutor

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


class UserDTO(BaseModel):
    id: int
    name: str


class FetchUsers(EntityQuery[int, UserDTO]):
    ids: frozenset[int]


@app.queries
def fetch_users(query: FetchUsers, session: DBSession) -> dict[int, UserDTO]:
    rows = session.execute(select(UserRow).where(UserRow.id.in_(query.ids))).scalars().all()
    return {row.id: UserDTO(id=row.id, name=row.name) for row in rows}


class TeamDTO(BaseModel):
    id: int
    owner: Annotated[UserDTO | None, Resolve(FetchUsers)]


class FetchTeams(Query[list[TeamDTO]]):
    pass


@app.queries(FetchTeams)
def fetch_teams(session: DBSession) -> list[dict]:
    return [dict(row) for row in session.execute(select(TeamRow)).mappings().all()]


@fastapi_app.get('/teams')
def list_teams(
    sync_query_executor: Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)],
) -> list[TeamDTO]:
    return sync_query_executor.fetch(FetchTeams())
```

The `fetch_teams` handler honestly returns rows. fastbff reads `Query[list[TeamDTO]]`
to know the output target, notices `TeamDTO` has a `Resolve` field, and runs the resolve
pipeline inside the executor so the endpoint receives validated DTOs.

The `DBSession` alias is a plain FastAPI `Depends(...)`. fastbff collects every handler's
and resolver's `Depends` params into one factory, so FastAPI-style `Depends` parameters
resolve at request time exactly as they would in a FastAPI route handler. The same
`Session` instance is reused across every query and resolver in a single request.

Spell out the `Annotated[..., Depends(...)]` form at every use site — FastAPI walks the
`Annotated` metadata and resolves a fresh executor per request (per-request cache,
per-request absence tracking). Override providers in tests via FastAPI's standard
`fastapi_app.dependency_overrides`, or `app.bind(...)`.

### SQLAlchemy extension

Optional extra — install with `pip install fastbff[sqlalchemy]`. The
`fastbff.sqlalchemy.SqlalchemyConverter` removes the manual `[{...} for row in
scalars]` loop inside `@queries` handlers:

```python
from fastbff.sqlalchemy import SqlalchemyConverter

def make_sqlalchemy_converter(session: DBSession) -> SqlalchemyConverter:
    return SqlalchemyConverter(session)

SqlalchemyConverterDep = Annotated[SqlalchemyConverter, Depends(make_sqlalchemy_converter)]


@app.queries(FetchTeams)
def fetch_teams(sqlalchemy_converter: SqlalchemyConverterDep) -> list[TeamDTO]:
    statement = select(TeamRow.id, TeamRow.owner_id.label('owner'))
    return sqlalchemy_converter.execute_all(statement, list[TeamDTO])
```

The converter executes the `Select` and projects rows into the shape the resolve pipeline
expects — column labels in the `Select` must match field names on the target model
(`owner_id` labelled `owner` so `Resolve(FetchUsers)` finds its key). The declared return
type (`list[TeamDTO]`) describes what the *caller* receives after rendering; the converter
is row-shaped under the hood. Use `execute_one` for `Query[Model]` (single-model) handlers.

### Testing with `QueryExecutorMock`

`QueryExecutorMock` builds via `QueryExecutor.create` with the app's `query_annotations`
index. Stubbed queries return the canned value; un-stubbed queries fall through to the
real `@queries` handler. Stubs are honoured by both direct `await mock.fetch(...)` calls
and by the render pipeline (a `Resolve(FetchUsers)` field fetches through the same
override), so a stubbed `EntityQuery` short-circuits its handler:

```python
import asyncio
from fastbff import QueryExecutorMock

mock = QueryExecutorMock.create(query_annotations=app.query_annotations)
mock.stub_query(FetchUsers, {10: UserDTO(id=10, name='u10')})

result = asyncio.run(mock.fetch(FetchUsers(ids=frozenset({10}))))
assert result == {10: UserDTO(id=10, name='u10')}
mock.reset_mock()  # clear stubs; subsequent fetch() calls hit real @queries handlers
```

## Errors

All errors raised by the library subclass `FastBFFError`:

- `RegistrationError` — base class for the registration-time errors below.
  - `QueryRegistrationError` — bad `@queries` declaration (missing return type,
    return type does not match `Query[T]`, multiple `Query[T]` parameters, an
    `EntityQuery` without a discoverable ids field) or a duplicate registration of the
    same query function or query type (raised by both `@app.queries` and
    `app.include_router`).
  - `ResolveRegistrationError` — bad `Resolve(...)` field (neither or both of a query
    type and `resolver=` supplied, more than one `Resolve` on a field, or a
    `Resolve(QueryType)` whose target is not a registered `EntityQuery`). Raised at
    finalize / mount time.
- `QueryNotRegisteredError` — `QueryExecutor.fetch` received a query class
  with no registered handler. Subclasses `KeyError` for back-compat.
- `CacheKeyError` — a `Query` field value cannot be turned into a cache key
  (unhashable and not a shape fastbff can normalise). Subclasses `TypeError`.

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
uv run ty check fastbff         # type check
uv run pre-commit install       # install git hooks
uv run pre-commit run --all-files
```

Tests are colocated with the modules they exercise, using the `_test.py`
suffix (e.g. `fastbff/query_executor/query_executor_test.py`).
Integration tests that assemble a real FastAPI + SQLAlchemy + SQLite app
live in `integration_tests/`.

## License

MIT
