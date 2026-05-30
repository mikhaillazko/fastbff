# ADR 0001 — DI rework: how to close TODO P0 #2

| Field      | Value                                       |
|------------|---------------------------------------------|
| Status     | Proposed                                    |
| Deciders   | maintainers                                 |
| Date       | 2026-05-04                                  |
| Supersedes | —                                           |
| Related    | `TODO.md` P0 #2 (DI integration coupling)   |

## Context

`fastbff` integrates with FastAPI's DI by synthesising a single
`provide_query_executor` factory at finalize time. The factory's
`__signature__` is patched to declare the union of every registered
handler's `Annotated[..., Depends(...)]` parameters as keyword-only
params. FastAPI's `get_dependant` reads `__signature__`, resolves the
graph, and hands the values to a per-request `QueryExecutor`.

This works, but couples to private FastAPI surface in two places:

1. `provide_query_executor.__signature__ = Signature(parameters=...)`
   — runtime mutation of introspection metadata
   (`fastbff/di.py:149`).
2. `QueryExecutor.__signature__ = Signature(parameters=[])` — silences
   FastAPI's introspection of `QueryExecutor.__init__` when an
   endpoint declares `Depends(QueryExecutor)`
   (`fastbff/query_executor/query_executor.py:116`).

The offline DI path (`@app.entrypoint`) used to be a third coupling
point — it imported `solve_dependencies`, `get_dependant`, and
synthesised a `Request` with private scope keys. That path was
removed in commit `f8d6851`, eliminating four of the six original
coupling points.

What remains is the question of whether to leave the synthesised
factory pattern alone, refine it cosmetically, or replace it with a
different mechanism for declaring the dep union to FastAPI.

### Constraints

The DX must stay as it is today:

- Handlers and transformers declare deps as
  `Annotated[T, Depends(factory)]` parameters — no fastbff-specific
  marker.
- Endpoints declare
  `Annotated[QueryExecutor, Depends(QueryExecutor)]` — no synthesised
  symbol the user has to import.
- `app.bind(target, factory)` works as a thin wrapper over
  `dependency_overrides`.

These constraints rule out any approach that requires the user to
list deps a second time at the endpoint, or to import a private
symbol like `provide_query_executor`.

### Why not "just lean on FastAPI's public API entirely"

FastAPI's resolver is signature-driven. To resolve N deps, *some*
function's signature must declare those N deps. There is no public
hook to feed deps to FastAPI from outside a callable's signature.

So every option below either (a) keeps the synthesised-signature
pattern in some form, (b) moves the deps onto a different surface
that FastAPI already inspects (route-level `dependencies=`,
sub-routers), or (c) replaces FastAPI's resolver with our own.

## Options

### Option 0 — Status quo

Keep `__signature__` mutation on both `provide_query_executor` and
`QueryExecutor`. Update `pyproject.toml` to reflect the
post-`@entrypoint`-removal floor and add a regression test.

**Pros**
- Zero work. Already shipping.
- Behaviour is well-understood by current contributors.
- Per-request resolution touches only public FastAPI surface
  (`Depends`, `dependency_overrides`); the only private bit is
  *reading* what `get_dependant` does with `__signature__`.

**Cons**
- Two `__signature__ =` lines in the codebase. Looks magical to
  newcomers.
- `provide_query_executor.__signature__ = Signature(parameters=...)`
  relies on FastAPI's `get_dependant` continuing to prefer
  `__signature__` over `__annotations__`. Not documented as stable.
- All endpoints pay for the union of every registered handler's deps,
  even ones they don't use — wastes work for heavy deps that only a
  few queries need.

### Option A — `exec`-built factory (cosmetic)

Generate `provide_query_executor`'s source as a real Python `def`
instead of patching `__signature__`:

```python
src = (
    "def provide_query_executor("
    + ", ".join(f"{spec.name}: {ann_repr} = Depends({factory_repr})"
                for spec in specs)
    + "): return _build(...)"
)
exec(src, globals_ns, local_ns)
```

The function then has a real signature; nothing is mutated.

**Pros**
- Removes one of the two `__signature__ =` lines.
- Generated function is indistinguishable from a hand-written one to
  any introspection — `inspect.signature`, `__annotations__`, IDE
  tools all see a real signature.
- Smallest possible change. No test fallout.

**Cons**
- Strictly cosmetic — same coupling to `get_dependant`'s behaviour,
  same union-of-deps cost on every request.
- `exec`-generated source has its own ergonomic costs: harder to set
  breakpoints in, requires careful escaping when constructing the
  source string.
- `QueryExecutor.__signature__ = Signature([])` still required — the
  empty-signature trick is irreducible under the current DX
  (FastAPI's `get_dependant` still introspects the class referenced
  in `Depends(QueryExecutor)` before override substitution).

### Option B — Route-level `dependencies=` + `request.state`

Don't put the union on one factory. Instead, attach each unique dep
to each route as a side-effect dependency that captures its resolved
value into `request.state`:

```python
def make_capturer(factory):
    def capturer(
        request: Request,
        value: Annotated[Any, Depends(factory)],
    ) -> None:
        request.state.fastbff_resolved[factory] = value
    return capturer

# during app.mount(fastapi_app):
deps = [Depends(make_capturer(f)) for f in self._unique_factories()]
for route in fastapi_app.routes:
    route.dependencies.extend(deps)
```

`provide_query_executor` becomes a plain function:

```python
def provide_query_executor(request: Request) -> QueryExecutor:
    return QueryExecutor(
        query_annotations=...,
        resolved_deps=request.state.fastbff_resolved,
        handler_index=...,
    )
```

**Pros**
- No `__signature__` mutation on `provide_query_executor`. Each
  capturer is a normal function with a normal signature.
- Uses only public FastAPI surface (`route.dependencies`,
  `request.state`).
- Capturers are individually small and composable.

**Cons**
- `app.mount(fastapi_app)` becomes invasive: walks
  `fastapi_app.routes`, mutates each route's `dependencies` list,
  rebuilds each route's dependant tree. Routes added *after* `mount`
  don't get the capturers.
- Sub-routers / nested apps need recursive handling.
- **DX trap**: every route on the FastAPI app gets the fastbff
  capturers attached — including routes that don't use fastbff. A
  typo in some unrelated dep factory will start failing requests on
  unrelated routes because the dep graph now includes everything.
- `request.state` is per-request mutable state with no type guarantees.
  Adds a new failure mode: missing keys at fetch time if the capturer
  didn't run for some reason.
- Still resolves the full union per request — does not solve the
  per-endpoint scoping problem, just relocates the synthesis.

### Option C — Own the DI graph

Walk registered handlers ourselves, resolve `Depends(...)` via a tiny
container that understands FastAPI-style
`Annotated[..., Depends(factory)]` parameters. Stop reaching into
`fastapi.dependencies.utils` entirely.

**Pros**
- Zero coupling to FastAPI internals. We could in principle support
  FastAPI ≥0.100 indefinitely.
- Full control: per-handler, per-endpoint, async/sync, generator
  cleanup, caching semantics — all ours to define.
- Enables features that are hard with FastAPI's resolver: e.g.,
  parallel resolution of independent deps, custom scopes
  (per-fetch vs per-request).

**Cons**
- Significant scope. We'd have to re-implement `use_cache=True`,
  sub-dependency resolution, generator deps (sync + async),
  request-scoped exit stacks, and any future FastAPI Depends
  semantics our users come to expect.
- Subtle behaviour drift from FastAPI's own resolver is a real risk
  — users will reasonably expect identical semantics.
- Deps that integrate with FastAPI's request lifecycle (e.g.,
  `Depends(get_db)` that yields under a per-request `AsyncExitStack`)
  need to thread through *our* exit stack, which means we re-implement
  that infrastructure too.
- `collect_dep_specs` covers ~30% of the work. Realistic estimate:
  one week of careful implementation + testing.

### Option D — `QueryExecutor[Q1, Q2, ...]` per-endpoint scoping

Parameterise `QueryExecutor` with the queries an endpoint will fetch.
Each parameterisation is its own `dependency_overrides` key with its
own factory whose signature lists only that subset's transitive deps.

```python
@fastapi_app.get('/teams')
def list_teams(
    qe: Annotated[QueryExecutor, Depends(QueryExecutor[FetchTeams])],
) -> list[TeamDTO]:
    return qe.fetch(FetchTeams())
```

`QueryExecutor[...]` returns a frozen, hashable, callable
`_QueryExecutorAlias`. At `app.mount(fastapi_app)`, walk routes,
collect every distinct alias, compute each one's transitive dep
closure, and register a per-alias override in
`fastapi_app.dependency_overrides`.

**Pros**
- Per-endpoint scoping. An endpoint that uses one query doesn't
  resolve heavy deps required by other queries.
- Type-level documentation of what each endpoint can fetch — readable
  at the route signature without jumping to the body.
- Adding a new query with a heavy dep no longer slows every endpoint;
  only endpoints that declare it pay.
- Override registration uses public FastAPI surface entirely
  (`dependency_overrides[alias] = factory`).

**Cons**
- Per-endpoint scoping requires knowing each endpoint's **transitive
  dep closure**. Static analysis can find which transformers are
  reachable from a `Query[T]`'s return type via `T`'s
  `Annotated[..., TransformerAnnotation]` fields. Static analysis
  cannot find which queries each transformer's body will fetch via
  `executor.fetch(...)` calls — that's runtime. Resolution paths:
    - **Explicit user listing**: `QueryExecutor[FetchTeams,
      FetchUsers]`. Verbose; missed declarations only surface as
      runtime errors.
    - **`@transformer(uses=[FetchUsers])`**: explicit on the
      transformer side. Redundant with the body, drifts over time.
    - **AST inspection of transformer bodies**: works for the common
      `executor.fetch(SomeQuery(...))` pattern, brittle for any
      indirection.
    - **Runtime error when undeclared query is fetched**: clear
      message, but moves the failure mode from "endpoint slow because
      of unused deps" to "endpoint broken because forgot to declare".
- `__class_getitem__` returning a callable instance plays awkwardly
  with type checkers. The annotation
  `Annotated[QueryExecutor, Depends(QueryExecutor[FetchTeams])]`
  declares `QueryExecutor` as the runtime type but
  `Depends(...)` receives a `_QueryExecutorAlias`. Will likely need
  `cast` or a custom plugin to keep ty/mypy clean.
- `app.mount` still has to walk routes (same invasiveness as Option
  B, but per-alias rather than per-factory). Routes added after mount
  miss the override unless re-mounted.
- The DX shifts: every endpoint declares its query types. For
  multi-query endpoints, the type list grows. Refactors that move a
  `fetch` call into a helper require updating the type parameter on
  every caller.

### Option E — Registration-time fetch-target validator (AST)

> **Scope note.** Unlike A–D, this option does **not** rework the DI
> surface. The two `__signature__` mutations from the Context section
> remain. It addresses a separate class of failure — request-time
> `QueryNotRegisteredError` raised from inside a transformer — by
> moving detection to `FastBFF.finalize()`. Listed here because the
> mechanism (static AST walk of transformer bodies) is the same one
> Option D would need for transitive-closure inference, so a decision
> on D either reuses or supersedes this work.

Walk each registered transformer's body with `ast`, find
`<executor>.fetch(<QueryCls>(...))` calls, resolve the class against
the function's `__globals__` and closure cells, and at `finalize()`
raise `TransformerRegistrationError` for any fetched `Query` subclass
not in the app's registry.

```python
@app.transformer
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],
    qe: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> User | None:
    return qe.fetch(FetchUsers(ids=batch.ids)).get(owner_id)

# `FetchUsers` not registered → app.finalize() raises with a message
# pointing at the transformer, instead of the first request to a route
# whose response model uses this transformer.
```

Recognised idioms: direct call, aliased executor parameter,
single-assignment `q = FetchUsers(...); qe.fetch(q)`, and class lookup
through closure cells in addition to module globals. Documented
silent misses: `self.qe.fetch(...)` (Attribute receiver), reassigned
locals, anything that escapes intra-function reasoning.

| Pros | Cons |
|------|------|
| Surfaces a runtime error class at composition time — matches the existing project rule that `@queries`/`@transformer` mistakes blow up at registration. | Does not address TODO P0 #2. Both `__signature__` mutations remain unchanged. |
| Best-effort by construction: silent miss, no false positives. Safe to land without a deprecation window. | Best-effort is also a weakness — passing the validator is not a guarantee. Users may read it as one. |
| Small scope (~150 LOC + tests). No new public surface; the validator is internal to `finalize()`. | Bound to `inspect.getsource`. Transformers defined in the REPL, via `exec`, or as lambdas are not seen. |
| Reuses existing helpers — `_iter_depends_params` and `_is_query_executor_dep` from `fastbff/di.py` identify the executor parameter; no new DI logic. | Recognised-idiom set is fixed in code. New patterns (e.g. an `executor.fetch_many(...)` API) require a discovery update. |
| Provides the static-closure primitive Option D would consume for transitive-dep inference. Lands the building block before committing to D. | Adds an AST pass per `finalize()` call. Cheap, but non-zero on cold start; cached implicitly via `_finalized_for`. |
| Zero DX impact. Users write transformers exactly as they do today. | If Option D ships later with the same primitive expanded into closure inference, this validator becomes redundant (the closure inference subsumes it). |

## Recommendation

**Phase 1 — ship Option A.** It is the cheapest credible answer to
the surface concern in TODO P0 #2 (one fewer `__signature__` line,
generated factory looks like real Python). No behaviour change, no
test fallout, no DX impact. Estimate: half a day including tests.

**Phase 2 — design Option D as a separate experiment.** It is the
only option that improves something users feel (per-endpoint
resolution cost, type-level documentation). The transitive closure
problem is the only real design question; prototype it with explicit
listing first (no closure inference) and measure DX before adding
inference. If the explicit form feels acceptable, ship it. If not,
pick (b) or (c) for inference. Do not commit to D before the
prototype.

**Phase 3 — only consider Option C if Option D's prototype reveals
that we want fastbff to evolve faster than FastAPI's Depends
semantics allow.** This is unlikely. C is a non-trivial rewrite for a
benefit (zero version coupling) that has not bitten us in real
usage.

**Reject Option B.** It pays for route rewriting (the cost of D's
mount step) without buying per-endpoint scoping (the value of D). The
`request.state` plumbing introduces a new failure mode without a
matching upside.

## Decision

To be filled in after maintainer review.
