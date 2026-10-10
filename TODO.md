# TODO — release-readiness review

Goal: ship `fastbff` in a state where a developer who has never seen it can
adopt it without footguns. North star is **simple to use, hard to misuse**.

Items are ordered by user-impact, not implementation cost. Each one calls out
the file/symbol to touch so the next contributor can start without a re-review.

Completed items have been removed; check `git log` for the fix details.

---

## P0 — credibility blockers (a new user gives up in 30 seconds)

Async handlers (was #1, "silent corruption") shipped in 0.2.0 and were then
reworked in **0.3.0** into an async-native core with an explicit `Resolve`
render pipeline (ADR 0002; `docs/adr/0002-async-core-and-resolve-phase.md`).
The old inter-query-concurrency follow-up is closed — independent `Resolve`
fields now fetch concurrently via `asyncio.gather` in `fastbff/resolve.py`.

### 2. DI integration — correctness fixes complete; endpoint scoping remains optional

The injection plumbing used to hand-edit Python's introspection metadata
in two places to make `Depends(...)` work the way we want. See
`docs/adr/0001-di-rework.md` for the full options analysis.

Sites:

- ~~`QueryExecutor.__signature__ = Signature(parameters=[])`~~ —
  **resolved**. `QueryExecutor.__init__` is now
  parameterless, so `inspect.signature(QueryExecutor)` is naturally
  empty; no `__init__` params leak in when an endpoint declares
  `Depends(QueryExecutor)`. Populated executors are built via
  `QueryExecutor.create(...)`. Guarded by
  `test_executors_have_empty_signature`.
- `fastbff/di.py:build_provide_query_executor` (and the sync facade provider) —
  `provide_query_executor.__signature__ = Signature(parameters=...)`.
  Synthesises a function signature listing the union of every
  registered handler's deps so FastAPI resolves them all at once.
  Kept deliberately: assigning `__signature__` is PEP 362, the
  standard way to give a generated callable a programmatic signature
  (Pydantic does the same for model `__init__`). `inspect.signature`
  — the only thing FastAPI reads — honors it by spec, so this is not
  coupling to FastAPI internals.

Each dependency occurrence now retains its original declaration; FastAPI owns
caching, implicit factories, security scopes, and yield lifetimes. Regression
tests live in `fastbff/di_test.py`. ADR 0001 records the accepted decision and a
review of FastAPI 0.143.0's injection implementation.

**Remaining options** (none required for the signature-generation concern):

- **Per-endpoint scoping** (revised ADR Option D). Prototype a direct provider
  such as `Depends(app.executor_for(Q1, Q2))` with explicit query roots. Compare
  dependency isolation, request cost, and OpenAPI output before committing.
- **Own the DI graph** (ADR Option C). Defer unless a concrete non-HTTP use case
  justifies owning dependency caching, request injection, and resource lifetimes.

---

## P1 — silent footguns

`bind()` propagation and executor-override precedence are fixed. Bindings update
every mounted app; finalization preserves explicit overrides. Later registrations
require remounting each host, while existing providers retain consistent snapshots.
See `fastbff/app_test.py` and the README's dependency-injection section.

---

## P2 — packaging and release process

Shipped in 0.2.0: Beta status + version bump (`pyproject.toml`), `__version__`
from package metadata (`fastbff/__init__.py`), `CHANGELOG.md` (Keep-a-Changelog),
and a tag/version guard in `publish.yml`. CI hardening (SHA-pinned actions,
least-privilege perms, coverage + Codecov, `pip-audit`, CodeQL, dependabot,
`SECURITY.md`) also landed. See `git log`.

### 4. No docs site

Optional but recommended: a docs site (mkdocs-material) for the cookbook +
reference, separate from the README. (`CONTRIBUTING.md` and a manual-dispatch
TestPyPI dry-run workflow, `.github/workflows/testpypi.yml`, shipped — see
`git log`.)

---

## P3 — ergonomics / nits

- `@app.queries(FetchAllUsers)` (decorator-factory form) vs
  `@app.queries` is a subtle API split. Document the decision tree, or
  detect parameterless handlers and emit a clear error pointing at the
  explicit form when the user forgets.
- `FastBFF` itself is usable as a `dependency_overrides_provider`.
  Document this — it makes custom test harnesses easier. (Likely
  subsumed by the P0 #2 rework.)

---

## Test coverage gaps

Cases that bite first-time users; we should have at least one regression
test per row:

- ~~Async handler / async transformer.~~ — supported + covered by
  `query_executor_test.py`.
- `validate_batch` over a large page (sanity / performance smoke).
