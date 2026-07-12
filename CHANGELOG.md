# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-13

Ground-up rework of the executor core and composition model (ADR 0002). This is
a **breaking** release; see `docs/migration/0.2-to-0.3.md`.

### Added

- **Async-native `QueryExecutor`.** `QueryExecutor.fetch` is now a coroutine.
  Async endpoints `await query_executor.fetch(query)`. Async handlers run
  directly on the event loop; sync handlers are offloaded to one anyio worker
  thread. Async composition never exhausts the worker-thread pool.
- **`SyncQueryExecutor`** — a sync facade for sync endpoints. Inject
  `Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)]` and call
  `sync_query_executor.fetch(query)`; it bridges onto the loop.
- **`Resolve` field annotation** replaces validator-driven transformers.
  `Annotated[T, Resolve(SomeEntityQuery)]` or `Annotated[T, Resolve(resolver=fn)]`
  declares how a relation field is populated. Composition runs as an explicit
  plan → concurrent-fetch → merge pipeline owned by the executor, so
  `model_validate` is context-free and DTOs are testable as plain models.
  Independent fields fetch concurrently (`asyncio.gather`); nested models
  resolve depth-first, one bulk fetch per field per level.
- **`EntityQuery[K, V]`** makes entity-level caching an explicit opt-in. A plain
  `Query[dict[K, V]]` now gets call-level caching only.
- **In-flight de-duplication.** Concurrent identical `fetch`es share one
  `asyncio.Future`; a backend query runs at most once per key per request.
- Resolvers are discovered automatically from the `Resolve` fields of a query's
  response model — their `Depends(...)` params join the DI union with no extra
  decorator.

### Changed

- `QueryCache` is a single async implementation; the sync/async twin methods and
  the thread lock are gone (the cache is only touched on the loop).
- `SqlalchemyConverter` docs updated for the render pipeline; behaviour unchanged.

### Removed

- **`@app.transformer` / `@router.transformer`, `BatchArg`,
  `build_transform_annotated`, `TransformerAnnotation`, `transformer_metadata`,
  `validate_batch`** — replaced by `Resolve`.
- **`QueryExecutor.afetch`** — `fetch` is async; sync endpoints use
  `SyncQueryExecutor`.
- **`AsyncDispatchError`, `BatchContextMissingError`,
  `TransformerRegistrationError`** — these failure modes no longer exist.
  `ResolveRegistrationError` is added for mis-declared `Resolve` fields.
- Shape-based entity-cache detection (a `dict[K, V]` return plus any iterable
  field). Use `EntityQuery[K, V]`.

## [0.2.0] - 2026-07-12

### Added

- **Async handlers and transformers.** `async def` query handlers and
  transformers are supported. Async FastAPI endpoints call
  `await query_executor.afetch(query)`; sync endpoints need no extra code and
  call `fetch` as before. Async handlers (and any nested `afetch` they await)
  run directly on the event loop, so async composition never exhausts the
  worker-thread pool, while a sync handler's whole subtree stays confined to one
  worker thread (preserving thread affinity for request-scoped resources such as
  a DB session). `anyio` is now a runtime dependency.
- **`fastbff.sqlalchemy.SqlalchemyConverter`** extension for turning SQLAlchemy
  result rows into DTOs (opt-in via the `sqlalchemy` extra).
- **`@queries(QueryType)`** decorator form for parameterless handlers.
- **Automatic result wrapping** through `validate_batch` when a handler's
  declared return type is a Pydantic model (or `list` thereof) with transformer
  fields — end users no longer call `validate_batch` directly.
- **`__version__`** on the package, read from installed metadata.
- **`AsyncDispatchError`** and **`CacheKeyError`** are re-exported from the
  package root.
- The package ships `py.typed`.

### Changed

- **Dependency injection** now uses FastAPI-native resolution; the offline-DI
  `@app.entrypoint` path was removed.
- **Cache keys hardened.** Pydantic models, dataclasses, and natively-hashable
  scalars are normalised into stable keys; an unhashable `Query` field raises
  `CacheKeyError` with guidance instead of a bare `TypeError` from cache
  internals.
- **Transformer fetch targets are validated at finalize/mount.** Fetching an
  unregistered query raises `TransformerRegistrationError` at composition time
  rather than surfacing as a request-time error (best-effort static discovery).
- Duplicate `@transformer` registration now raises, matching the `@queries` rule.
- `FastBFF.query_annotations` is exposed as a read-only view.
- Development status promoted to Beta.
- The publish workflow verifies the release tag matches the project version
  before building/publishing.

### Fixed

- **Entity-level cache correctness.** The cache bucket key now includes every
  `Query` field except the ids field, so two entity fetches that differ only in
  a discriminating field (e.g. `tenant_id`) no longer cross-serve each other's
  cached entries within a request.
- **Async dispatch safety.** A callable that hides an async body from
  `iscoroutinefunction` (e.g. an `async def __call__` object) now raises
  `AsyncDispatchError` instead of caching an unawaited coroutine.
- String / PEP 563 annotations are resolved, so modules using
  `from __future__ import annotations` work.
- Transformer fields are discovered on model subclasses.

## [0.1.0] - 2026-04-16

- Initial public release.

[0.2.0]: https://github.com/mikhaillazko/fastbff/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mikhaillazko/fastbff/releases/tag/v0.1.0
