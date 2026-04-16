# pydantic-bff

Simple back-end for front-end using Pydantic. Declarative data composition with typed
transformers, dependency injection, and automatic N+1 avoidance. Suitable for modular
monolithic systems.

## Features

- **Declarative data composition** — describe the shape of a response once on a Pydantic
  model; fetching happens automatically.
- **Typed queries** — `Query[T]` carries its own return type; `QueryExecutor.fetch(q)`
  always returns `T`, no casting.
- **Automatic N+1 avoidance** — transformers declare a `BatchArg[T]` and the framework
  plans a single bulk fetch per page instead of one call per row.
- **Two-level cache** — call-level (identical query args) plus entity-level (overlapping
  ID sets are merged into one fetch with only the missing ids).
- **Dependency injection** — built on FastAPI's `Depends`; the same `QueryExecutor` /
  repository / session is shared across every transformer in a request scope.
- **No custom `BaseModel`** — opt into batch introspection with a `@bff_model` class
  decorator on your own Pydantic models.

## Install

```bash
pip install pydantic-bff
```

Runtime deps: `pydantic>=2`, `fastapi>=0.100`. Python 3.12+ (uses PEP 695 generics).

## Three-phase execution

Every page of data flows through three phases, orchestrated by your handler:

```
Phase 1 — Plan    populate_context_with_batch(Model, rows)
                  → walks rows, collects every unique id for every BatchArg field
                    into {batch_key: set[ids]}

Phase 2 — Fetch   query_executor.fetch(BulkQuery(ids=...))
                  → one bulk @query call per batch, populating the QueryExecutor cache

Phase 3 — Merge   Model.model_validate(row, context=ctx) for each row
                  → each @transformer runs with dependencies injected; every
                    fetch() inside a transformer is a guaranteed cache hit
```

## Quickstart

```python
from dataclasses import dataclass
from pydantic import BaseModel

from src import (
    BatchArg,
    InjectorRegistry,
    QueriesRegistry,
    Query,
    QueryExecutor,
    TransformerRegistry,
    bff_model,
    build_transform_annotated,
    populate_context_with_batch,
)

# --- Domain -----------------------------------------------------------------

@dataclass(frozen=True)
class User:
    id: int
    name: str

# --- Wiring -----------------------------------------------------------------

injector = InjectorRegistry()
queries = QueriesRegistry(injector=injector)
transformer = TransformerRegistry(injector=injector)
executor = QueryExecutor(queries_registry=queries)

# Make Depends(QueryExecutor) resolve to our shared instance within the scope.
injector.dependency_provider.dependency_overrides[QueryExecutor.__origin__] = lambda: executor

# --- Bulk query -------------------------------------------------------------
# A Query's return type (dict[int, User]) is carried in Query[...].
# The framework uses it to key call- and entity-level caches.

class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]

@queries
def fetch_users(args: FetchUsers) -> dict[int, User]:
    # Replace with a real DB call, Redis lookup, HTTP request, etc.
    return {i: User(id=i, name=f'u{i}') for i in args.ids}

# --- Transformer -----------------------------------------------------------
# BatchArg[int] tells the planner to collect all `owner` ids for the current page
# into batch.ids, so the transformer can issue one bulk fetch for the whole page.

@transformer
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],
    ex: QueryExecutor,
) -> User | None:
    users = ex.fetch(FetchUsers(ids=batch.ids))  # cache hit after Phase 2
    return users.get(owner_id)

OwnerT = build_transform_annotated(transform_owner)

# --- Response model --------------------------------------------------------

@bff_model
class TeamDTO(BaseModel):
    id: int
    owner: OwnerT  # OwnerT already encodes `User | None` as its base type

# --- Handler ---------------------------------------------------------------

@injector.entrypoint
def render_teams_page() -> list[TeamDTO]:
    rows = [
        {'id': 1, 'owner': 10},
        {'id': 2, 'owner': 20},
        {'id': 3, 'owner': 10},  # duplicate id → still just one DB call
    ]

    # Phase 1 — Plan
    ctx = populate_context_with_batch(TeamDTO, rows)

    # Phase 2 — Fetch (one bulk call per batch field)
    for batch in TeamDTO.__batches__:
        executor.fetch(FetchUsers(ids=frozenset(ctx[batch.key])))

    # Phase 3 — Merge
    return [TeamDTO.model_validate(row, context=ctx) for row in rows]
```

A single page of N rows issues **one** `fetch_users(...)` call — regardless of N, and
regardless of how many duplicate ids the rows contain.

## Core concepts

### `Query[T]` + `@query`

A `Query[T]` subclass is a typed request object whose return type `T` is carried on the
class. Register a fetcher with the registry's decorator:

```python
class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]

@queries
def fetch_users(args: FetchUsers) -> dict[int, User]:
    ...
```

Return-type mismatches raise at registration time, not at runtime.

### `QueryExecutor.fetch(q)`

Per-request dispatcher with two caching layers:

- **Call-level** — identical query args return the cached result.
- **Entity-level** — for `dict[K, V]`-returning queries whose request has an ids field,
  overlapping ID sets are merged. A second `fetch(q)` with ids `{2, 3, 4}` after the
  first with `{1, 2, 3}` only fetches `{4}`. Absent ids (returned `{}` from the backend)
  are remembered too, so asking again doesn't hit the backend.

Absence is cached per-executor (per-request). Construct a fresh `QueryExecutor` to start
a new request.

### `@transformer` + `build_transform_annotated`

A transformer is a plain function with a return type annotation. Wrapping it into a
field annotation is a two-step dance:

```python
@transformer
def transform_owner(owner_id: int, ex: QueryExecutor) -> User | None:
    ...

OwnerT = build_transform_annotated(transform_owner)  # an Annotated[...] alias

class TeamDTO(BaseModel):
    owner: OwnerT
```

`OwnerT` is `Annotated[User | None, PlainValidator(transform_owner), ...]` — use it
directly as the field type. You can also compose with unions: `second_owner: OwnerT | None`.

### `BatchArg[T]`

Declaring a `BatchArg[T]` parameter on a transformer opts into bulk fetching:

```python
@transformer
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],      # all ids for this field on the current page
    ex: QueryExecutor,
) -> User | None:
    ex.fetch(FetchUsers(ids=batch.ids))
    ...
```

`batch.ids: frozenset[int]` contains every `owner` id across the whole page. The
transformer calls `fetch(FetchUsers(ids=batch.ids))` — Phase 2 already populated the
cache, so this is a hit, not a backend call.

### `@bff_model` + `populate_context_with_batch`

`@bff_model` inspects your Pydantic model for transformer fields with a `BatchArg` and
caches the batching metadata on `Model.__batches__`. No custom base class — it's a
normal `BaseModel` with a class decorator.

`populate_context_with_batch(Model, rows)` walks rows, collects every batch's ids into
`{batch_key: set[ids]}`, and returns a dict suitable to pass as Pydantic's validation
`context`. Phase 2 uses those ids to pre-warm the cache; Phase 3 reads them back via
`ValidationInfo.context` inside each `BatchArg`.

### Dependency injection

`InjectorRegistry` wraps FastAPI's `Depends`. Registration decorators
(`TransformerRegistry`, `QueriesRegistry`) automatically wrap your callables so
FastAPI-style dependencies resolve at call time:

```python
injector = InjectorRegistry()
queries = QueriesRegistry(injector=injector)

@queries
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, User]:
    # `session: DBSession` is Annotated[Session, Depends(get_session)] elsewhere
    ...

@injector.entrypoint
def handler() -> ...:
    # `entrypoint` opens a fresh dependency scope for this call
    ...
```

Override providers in tests with
`injector.dependency_provider.dependency_overrides[Service] = lambda: fake`.

### Testing with `QueryExecutorMock`

```python
from src import QueryExecutorMock

mock = QueryExecutorMock(queries_registry=queries)
mock.stub_query(FetchUsers, {10: User(id=10, name='u10')})

assert mock.fetch(FetchUsers(ids=frozenset({10}))) == {10: User(id=10, name='u10')}
mock.reset_mock()  # clear stubs; subsequent fetch() calls hit real @query handlers
```

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
(e.g. `src/query_executor/query_executor_test.py`). The cross-cutting
three-phase integration test lives at `integration_test.py` in the project root.

## License

MIT
