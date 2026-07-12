# fastbff

[![CI](https://github.com/mikhaillazko/fastbff/actions/workflows/ci.yml/badge.svg)](https://github.com/mikhaillazko/fastbff/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/mikhaillazko/fastbff/graph/badge.svg)](https://codecov.io/gh/mikhaillazko/fastbff)
[![PyPI](https://img.shields.io/pypi/v/fastbff.svg)](https://pypi.org/project/fastbff/)
[![Python versions](https://img.shields.io/pypi/pyversions/fastbff.svg)](https://pypi.org/project/fastbff/)
[![License: MIT](https://img.shields.io/pypi/l/fastbff.svg)](https://github.com/mikhaillazko/fastbff/blob/master/LICENSE)

Simple back-end for front-end using Pydantic. Declarative data composition with typed
transformers, dependency injection, and automatic N+1 avoidance. Suitable for modular
monolithic systems.

## Features

- **Declarative data composition** — describe the shape of a response once on a Pydantic
  model; fetching happens automatically.
- **Zero orchestration boilerplate** — `@queries` handlers return raw rows; the framework
  runs Plan + Fetch + Merge at the dispatch boundary.
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
pip install fastbff
```

Runtime deps: `pydantic>=2`, `fastapi>=0.100`. Python 3.12+ (uses PEP 695 generics).

## Quickstart

```python
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from fastbff import (
    FastBFF,
    BatchArg,
    Query,
    QueryExecutor,
    QueryRouter,
    build_transform_annotated,
)

# --- Domain -----------------------------------------------------------------

@dataclass(frozen=True)
class User:
    id: int
    name: str

# --- Router -----------------------------------------------------------------

router = QueryRouter()

# --- Bulk query -------------------------------------------------------------

class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]

@router.queries
def fetch_users(args: FetchUsers) -> dict[int, User]:
    return {i: User(id=i, name=f'u{i}') for i in args.ids}

# --- Transformer + Response model ------------------------------------------

@router.transformer
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> User | None:
    users = query_executor.fetch(FetchUsers(ids=batch.ids))
    return users.get(owner_id)

UserTransformerAnnotated = build_transform_annotated(transform_owner)

class TeamDTO(BaseModel):
    id: int
    owner: UserTransformerAnnotated

# --- Page-rendering query --------------------------------------------------
# `Query[list[TeamDTO]]` is the output contract; the handler returns honest
# rows (`list[dict[str, Any]]`) and the framework validates them to TeamDTO
# at the dispatch boundary, planning a single bulk `fetch_users` call for
# the whole page.

class FetchTeams(Query[list[TeamDTO]]):
    pass

@router.queries(FetchTeams)
def fetch_teams() -> list[dict[str, Any]]:
    return [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},  # duplicate id → still just one DB call
    ]

# --- HTTP route -------------------------------------------------------------

fastapi_app = FastAPI()


@fastapi_app.get('/teams', response_model=list[TeamDTO])
def list_teams(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    return query_executor.fetch(FetchTeams())

# --- Compose ----------------------------------------------------------------

app = FastBFF()
app.include_router(router)
app.mount(fastapi_app)
```

A single page of N rows issues **one** `fetch_users(...)` call — regardless of N, and
regardless of how many duplicate ids the rows contain. The handler honestly types its
return as `list[dict[str, Any]]`; `Query[list[TeamDTO]]` is the *output* contract that
`query_executor.fetch(...)` honors after running batch validation.

## Two-phase execution (under the hood)

When a `@queries` handler is registered with `Query[list[Model]]` (or `Query[Model]`)
where the model has transformer fields, fastbff runs Plan + Merge automatically inside
`query_executor.fetch(...)`:

```
Phase 1 — Plan    walks rows, collects every unique id for every BatchArg field
                  into a {batch_key: set[ids]} validation context

Phase 2 — Merge   Model.model_validate(row, context=ctx) for each row
                  → each @transformer runs with dependencies injected; the first
                    row's executor.fetch(...) issues one bulk call covering the
                    whole page, subsequent rows hit the entity-level cache
```

Handlers that already build model instances directly (e.g. `dict[int, User]` queries
constructing `User(...)` per row) flow through unchanged — already-validated values
are detected and the wrap is a no-op.

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

### `QueryExecutor.fetch`

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
`Annotated[ReturnType, TransformerAnnotation]` alias; bind it to a PascalCase
`<Name>TransformerAnnotated` name and use it directly as a field type:

```python
@app.transformer
def transform_user_id(
    user_id: int,
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> UserDTO | None:
  ...


UserTransformerAnnotated = build_transform_annotated(transform_user_id)


class TeamDTO(BaseModel):
  owner: UserTransformerAnnotated
```

The return type baked into the alias is exactly the function's declared return type
(including `Optional`, `list[...]`, etc.) — reuse the alias on as many models as you
like:

```python
class CommentDTO(BaseModel):
    author: UserTransformerAnnotated
```

### `BatchArg[T]`

Declaring a `BatchArg[T]` parameter on a transformer opts into bulk fetching. The
parameter carries the full set of ids for this field on the current page, collected
by Phase 1 of `validate_batch(...)`:

```python
@app.transformer
def transform_user_id(
    user_id: int,
    batch: BatchArg[int],            # all ids for this field on the current page
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> UserDTO | None:
    users = query_executor.fetch(FetchUsers(ids=batch.ids))
    return users.get(user_id)
```

The first row's `executor.fetch(FetchUsers(ids=batch.ids))` issues the bulk call;
subsequent rows hit the query executor's entity-level cache. One DB call per page,
regardless of row count.

### Dependency injection

fastbff defers to FastAPI's own DI: every registered handler is left as-is,
and at finalize time the app synthesises a single `provide_query_executor`
factory whose signature declares the union of every handler's
`Annotated[..., Depends(...)]` parameters. FastAPI resolves that graph
once per request and the executor hands the resolved values to each
handler / transformer at dispatch time.

```python
@app.queries
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, UserDTO]:
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

Declare your transformers, queries, and DTOs at module scope. fastbff
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
def fetch_users(args: FetchUsers) -> dict[int, UserDTO]: ...

@router.transformer
def transform_user_id(
    user_id: int,
    batch: BatchArg[int],
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> UserDTO | None: ...

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

`QueryExecutor` is request-scoped naturally: annotate handler parameters as
`Annotated[QueryExecutor, Depends(QueryExecutor)]` and FastAPI's own `Depends(...)`
pipeline will resolve a fresh instance per request. A complete route:

```python
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fastbff import (
  FastBFF, BatchArg, Query, QueryExecutor,
  build_transform_annotated,
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
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, UserDTO]:
  stmt = select(UserRow).where(UserRow.id.in_(args.ids))
  rows = session.execute(stmt).scalars().all()
  return {row.id: UserDTO(id=row.id, name=row.name) for row in rows}


@app.transformer
def transform_user_id(
    user_id: int,
    batch: BatchArg[int],
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> User | None:
  users_map = query_executor.fetch(FetchUsers(ids=batch.ids))
  return users_map.get(user_id)


UserTransformerAnnotated = build_transform_annotated(transform_user_id)


class TeamDTO(BaseModel):
  id: int
  owner: UserTransformerAnnotated


class FetchTeams(Query[list[TeamDTO]]):
  pass


@app.queries(FetchTeams)
def fetch_teams(session: DBSession) -> list[dict[str, Any]]:
    return list(session.execute(select(TeamRow)).mappings().all())


@fastapi_app.get('/teams', response_model=list[TeamDTO])
def list_teams(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
  return query_executor.fetch(FetchTeams())
```

The `fetch_teams` handler honestly returns rows. fastbff reads `Query[list[TeamDTO]]`
to know the output target, notices `TeamDTO` has transformer fields, and runs batch
validation inside `query_executor.fetch(...)` so the endpoint receives validated DTOs.

The `DBSession` alias is a plain FastAPI `Depends(...)` — fastbff's
`@app.queries` and `@app.transformer` decorators wrap your callable with the
injector, so FastAPI-style `Depends` parameters resolve at call time exactly
as they would in a FastAPI route handler. The same `Session` instance is
reused across every query/transformer in a single request.

Spell out the `Annotated[QueryExecutor, Depends(QueryExecutor)]` form at every use
site — FastAPI walks the `Annotated` metadata and resolves a fresh
`QueryExecutor` per request (per-request cache, per-request absence tracking).
Override providers in tests via FastAPI's standard
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

The converter executes the `Select` and projects rows into the shape fastbff's
auto-wrap expects — column labels in the `Select` must match field names on
the target model. The declared return type (`list[TeamDTO]`) describes what
the *caller* receives after auto-wrap; the converter is row-shaped under the
hood. Use `execute_one` for `Query[Model]` (single-model) handlers.

### Testing with `QueryExecutorMock`

`QueryExecutorMock` takes the app's `query_annotations` index. Stubbed
queries return the canned value; un-stubbed queries fall through to the
real `@queries` handler:

```python
from fastbff import QueryExecutorMock

mock = QueryExecutorMock(query_annotations=app.query_annotations)
mock.stub_query(FetchUsers, {10: UserDTO(id=10, name='u10')})

assert mock.fetch(FetchUsers(ids=frozenset({10}))) == {10: UserDTO(id=10, name='u10')}
mock.reset_mock()  # clear stubs; subsequent fetch() calls hit real @queries handlers
```

## Errors

All errors raised by the library subclass `FastBFFError`:

- `RegistrationError` — base class for the registration-time errors below.
  - `QueryRegistrationError` — bad `@queries` declaration (missing return type,
    return type does not match `Query[T]`, multiple `Query[T]` parameters)
    or a duplicate registration of the same query function or query type
    (raised by both `@app.queries` and `app.include_router`).
  - `TransformerRegistrationError` — bad `@transformer` declaration (missing
    return type, multiple `TransformerAnnotation` entries on a field),
    `build_transform_annotated` called on an unregistered function, or a
    duplicate `@transformer` registration on a single router or across an
    `include_router` merge.
- `QueryNotRegisteredError` — `QueryExecutor.fetch` received a query class
  with no registered handler. Subclasses `KeyError` for back-compat.
- `BatchContextMissingError` — transformer with a `BatchArg` was invoked
  without a batch context, almost always because a row was validated via
  plain `Model.model_validate` outside a fastbff dispatch boundary. Return
  rows from a `@queries` handler instead — the auto-wrap builds the batch
  context for you.

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
