# TODO — release-readiness review

Goal: ship `fastbff` in a state where a developer who has never seen it can
adopt it without footguns. North star is **simple to use, hard to misuse**.

Items are ordered by user-impact, not implementation cost. Each one calls out
the file/symbol to touch so the next contributor can start without a re-review.

Completed items have been removed; check `git log` for the fix details.

---

## P0 — credibility blockers (a new user gives up in 30 seconds)

### 1. Async handlers and transformers are accepted but broken

`QueryExecutor.fetch` (`fastbff/query_executor/query_executor.py:61`) and
`TransformerAnnotation._validate` (`fastbff/transformer/types.py:116`)
call handlers synchronously. An `async def fetch_users(...)` handler will
have its coroutine object cached and returned — silent corruption.

**Fix (proper)**: an `async fetch` path with parallel coroutine dispatch.
Larger scope — track separately once the rejection is in.

### 2. DI integration leans on FastAPI internals and signature-mutation hacks

The injection plumbing used to hand-edit Python's introspection metadata
in two places to make `Depends(...)` work the way we want. See
`docs/adr/0001-di-rework.md` for the full options analysis.

Sites:

- ~~`QueryExecutor.__signature__ = Signature(parameters=[])`~~ —
  **resolved** (Option F). `QueryExecutor.__init__` is now
  parameterless, so `inspect.signature(QueryExecutor)` is naturally
  empty; no `__init__` params leak in when an endpoint declares
  `Depends(QueryExecutor)`. Populated executors are built via
  `QueryExecutor.create(...)`. Guarded by
  `test_query_executor_has_empty_signature`.
- `fastbff/di.py:149` —
  `provide_query_executor.__signature__ = Signature(parameters=...)`.
  Synthesises a function signature listing the union of every
  registered handler's deps so FastAPI resolves them all at once.
  Kept deliberately: assigning `__signature__` is PEP 362, the
  standard way to give a generated callable a programmatic signature
  (Pydantic does the same for model `__init__`). `inspect.signature`
  — the only thing FastAPI reads — honors it by spec, so this is not
  coupling to FastAPI internals.

**Remaining options** (none required for the surface concern, which the
one legitimate `__signature__` line above does not represent):

- **Own the DI graph** (ADR Option C). Walk registered handlers and
  resolve `Depends(...)` ourselves. Zero version coupling, but a
  non-trivial rewrite for a benefit that has not bitten us.
- **`QueryExecutor[Q1, ...]` per-endpoint scoping** (ADR Option D).
  The only option that improves something users feel (per-endpoint
  resolution cost); prototype with explicit listing before committing.

---

## P1 — silent footguns

### 3. `bind()` after `mount()` does not propagate

`FastBFF.mount` (`fastbff/app.py:189`) does
`fastapi_app.dependency_overrides.update(self._overrides)` — a one-shot
copy. Subsequent `app.bind(...)` calls write to `self._overrides` only.
Users (especially in tests) expect post-mount binds to take effect.

**Fix options**:
- Have `mount` make `fastapi_app.dependency_overrides` and `self._overrides`
  the same dict (assignment-in-place via `clear() + update()` is risky;
  prefer rewriting `bind` to write to both if mounted).
- Or document loudly + raise on `bind` after `mount`.

---

## P2 — packaging and release process

### 4. `pyproject.toml` status is `Alpha` and version is `0.1.0`

`pyproject.toml:13`. For "wide developer use" this signals "do not depend
on this." Decide what stability bar we are committing to and bump.

### 5. `publish.yml` has no tag/version guard

`.github/workflows/publish.yml` runs `uv publish` on any release event
without checking the git tag matches `pyproject.toml` `version`, and
without a TestPyPI dry-run.

**Fix**:
- Add a step that fails if `pyproject.toml` `version` != `${GITHUB_REF_NAME#v}`.
- Optionally add a manual-dispatch TestPyPI workflow before promoting.

### 6. No `__version__` constant

`fastbff/__init__.py` should expose `__version__` (read from package
metadata via `importlib.metadata.version("fastbff")` so it stays in sync
with `pyproject.toml`).

### 7. No `CHANGELOG.md`, no `CONTRIBUTING.md`, no docs site

For wide adoption:
- `CHANGELOG.md` (Keep-a-Changelog format) so users can scan before
  upgrading.
- `CONTRIBUTING.md` covering the uv / ruff / ty toolchain that's documented
  in `CLAUDE.md` but invisible to outside contributors.
- Optional but recommended: a docs site (mkdocs-material) for the
  cookbook + reference, separate from the README.

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

- Async handler / async transformer (rejected at registration with a
  clear error).
- `validate_batch` over a large page (sanity / performance smoke).
- `bind()` called after `mount()` (whatever the chosen semantics).
