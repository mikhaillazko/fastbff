# ADR 0001 — DI rework: how to close TODO P0 #2

| Field      | Value                                       |
|------------|---------------------------------------------|
| Status     | Accepted — updated 2026-10-11                |
| Deciders   | maintainers                                 |
| Date       | 2026-05-04                                  |
| Supersedes | —                                           |
| Related    | `TODO.md` P0 #2 (DI integration coupling)   |

## Context

This context and the options below record the original proposal. The accepted
decision and FastAPI 0.143.0 review at the end supersede the old signature-removal
recommendation. Transformer references describe the pre-0.3 API; resolvers now
join the dependency graph through `Resolve` (ADR 0002).

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
    'def provide_query_executor('
    + ', '.join(f'{spec.name}: {ann_repr} = Depends({factory_repr})' for spec in specs)
    + '): return _build(...)'
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

Keep synthesized signatures and FastAPI-owned resolution. Implement correctness
and lifecycle fixes first; prototype endpoint scoping separately. Neither source
generation (Option A) nor a private dependency container (Option C) is needed.

## Decision

### Dependency declarations

`QueryExecutor` and `SyncQueryExecutor` have parameterless constructors, so
their class signatures need no override. The two generated provider functions
retain explicit `__signature__` metadata.

Every handler/resolver dependency occurrence gets a separate generated parameter.
Keep its original annotation and `Depends`/`Security` metadata. Do not deduplicate
using `(dependency, use_cache)`: implicit factories both start as `None`, uncached
occurrences must remain separate, and other metadata affects resolution.
FastAPI decides which occurrences share values. Executor self-injection remains
an internal sentinel, avoiding a circular dependency.

`use_cache=False` applies when FastAPI injects the executor, not on each query
dispatch. Per-fetch dependency lifetimes would require a separate design.

### Binding and mounting

- Track generated defaults separately from the effective override dictionary.
  Re-finalization replaces previous defaults and preserves explicit overrides,
  including those written directly to `dependency_overrides`.
- `bind()` updates fastbff and all currently mounted applications. Weak references
  avoid keeping discarded test applications alive.
- Each host retains its own dictionary. `mount()` updates matching keys without
  removing unrelated overrides; fastbff wins conflicts during mount or bind.
  Host-local test overrides remain possible through the host dictionary.
- Generated providers capture a query-registry snapshot alongside their dependency
  index. Later registration requires remounting each host. Old providers and
  existing executors keep a consistent graph. `finalize()` alone is local.
- Direct edits to fastbff's override dictionary propagate on mount; `bind()` is
  the API for immediate propagation. Already-resolved request values are unchanged.
- Endpoints using `Depends(provide)` directly retain that callable. Remounting
  updates the override used by `Depends(QueryExecutor)` and its sync facade.

### FastAPI implementation review (2026-10-11)

Latest published release reviewed: **0.143.0**, released October 8; the repository
lockfile currently uses **0.141.1**. See the
[release notes](https://fastapi.tiangolo.com/release-notes/#01430).

In [`dependencies/utils.py`](https://github.com/fastapi/fastapi/blob/0.143.0/fastapi/dependencies/utils.py),
`get_typed_signature()` uses `inspect.signature()`, and `get_dependant()` builds
the dependency tree. `solve_dependencies()` reads current overrides, rebuilds
replacement dependency trees, recursively resolves values, and manages generator
cleanup using request/function exit stacks. Calling that internal solver directly
would couple fastbff to private request-scope state.

In [`dependencies/models.py`](https://github.com/fastapi/fastapi/blob/0.143.0/fastapi/dependencies/models.py),
the cache key includes the callable, relevant OAuth scopes, and computed lifetime
scope. `use_cache` controls reuse rather than being part of the key. This is why
fastbff should forward complete declarations instead of maintaining its own key.

[`routing.py`](https://github.com/fastapi/fastapi/blob/0.143.0/fastapi/routing.py)
constructs route dependency graphs before requests. Our class-override approach
supplies its generated graph at request time; direct provider dependencies would
make that graph available when routes are constructed.

### Future evolution — proposals, not implemented

1. Prototype `Depends(app.executor_for(FetchTeams, FetchUsers))` returning a stable
   callable. Compare it with the current class override: dependency calls,
   reflection cost, OpenAPI output, and ergonomics. Avoid scanning or rewriting
   routes. The current `mount()` return value already allows experiments with a
   direct provider for the full registry.
2. Infer query reachability from `Resolve(QueryType)`. Require explicit roots for
   queries fetched from arbitrary handler/resolver bodies; do not assume reflection
   can discover them. Report undeclared fetches clearly. Unused dependencies can
   fail requests as well as add latency, so measure both isolation and performance.
3. Add a dependency-version CI matrix alongside the Python matrix: a verified
   lower bound, the lockfile, and latest releases. Include caching, security,
   sync/async generator cleanup, streaming lifetimes, and override behavior.
4. Keep FastAPI responsible for cleanup and request injection. Only consider an
   independent container for a concrete non-HTTP use case with explicit semantics.
