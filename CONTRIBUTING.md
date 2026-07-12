# Contributing to fastbff

Thanks for your interest in improving fastbff! This guide covers the local
toolchain and the conventions CI enforces, so your first pull request lands
green.

## Design principles

Before changing the public surface, skim [`CLAUDE.md`](CLAUDE.md) — it captures
the three rules the API is held to:

1. **Simplicity in DX.** Judge every feature by what the end developer has to
   write. Move work into the framework rather than asking the user to spell it
   out.
2. **Typing everywhere, no `# type: ignore`.** The public surface and the
   examples must type-check cleanly without ignores or casts. If an idiom needs
   `# type: ignore`, that's a signal the design is wrong — change the API.
3. **Annotation + reflection over duplicated declaration.** When a fact is
   already in a type annotation, recover it with `typing.get_type_hints` /
   `get_args` / `get_origin` instead of asking the user to declare it twice.

`CLAUDE.md` also has an architecture tour (composition root, the two cache
layers, the anyio async bridge) worth reading before touching the executor.

## Prerequisites

- **Python 3.12+** — the codebase uses PEP 695 generics (`class Foo[T]:`,
  `def f[F: Callable](...)`). The local version is pinned in `.python-version`.
- **[uv](https://docs.astral.sh/uv/)** for environments and dependencies.

## Getting started

```bash
git clone https://github.com/mikhaillazko/fastbff
cd fastbff
uv sync                 # create .venv and install the project + dev deps
uv run pre-commit install   # optional: run the checks on every commit
```

## Running the checks

CI runs exactly what `pre-commit` runs, so the fastest way to match CI is:

```bash
uv run pre-commit run --all-files
```

Or run each tool directly:

```bash
uv run pytest                                    # full suite (unit + integration)
uv run pytest fastbff/router_test.py             # one module
uv run pytest fastbff/router_test.py::test_name  # one test
uv run pytest --cov=fastbff --cov-report=term-missing  # with coverage

uv run ruff check . --fix     # lint + autofix
uv run ruff format .          # format
uv run ty check fastbff       # type check (the package only, not tests)
```

Coverage has a floor (`fail_under` in `pyproject.toml`); new code should not
drop total coverage below it. Ratchet the floor up when you meaningfully raise
coverage.

The CI matrix runs Python **3.12, 3.13, and 3.14**.

## Test layout and conventions

`pytest` discovers `*_test.py` files under both `fastbff/` and the repo root
(configured in `pyproject.toml`):

- **Unit tests** are colocated with the module they exercise
  (`fastbff/router.py` → `fastbff/router_test.py`).
- **Integration tests** live in `integration_tests/` and assemble a real
  fastbff app on FastAPI + SQLAlchemy + SQLite. `integration_tests/sample_app.py`
  is the shared fixture; tests drive it through `TestClient`.
- Sdist/wheel builds exclude `**/*_test.py`.

Follow the existing test style:

- **Name test files for their module:** `foo.py` → `foo_test.py`.
- **Prefix test-local classes with an underscore** (`_User`, `_FetchUsers`,
  `_TeamDTO`) so they read as private fixtures, not public API.
- **Use mnemonic literals** so an assertion is easy to verify at a glance —
  e.g. `_FetchA(key='a')` returning `'a:a'`, rather than opaque values.

## Code style

`ruff` is configured (see `pyproject.toml`) with:

- **`force-single-line = true`** for imports — one import per line. Don't
  reformat into parenthesised import groups.
- **single-quote** string formatting.
- line length 120 (`E501` is ignored, but keep lines reasonable).

`ty` runs in `warn` mode for a few rules that conflict with Pydantic/FastAPI
runtime dynamism (`unresolved-attribute`, `invalid-return-type`,
`invalid-type-form`, `invalid-method-override`). Don't tighten those without
checking what breaks.

## Commits and pull requests

- Keep commits focused; write imperative, present-tense subjects
  ("Harden cache keys", "Guard publish workflow against tag mismatch") to match
  the existing history.
- Add or update a regression test with any bug fix or behaviour change.
- Update `CHANGELOG.md` (Keep a Changelog format) under an `Unreleased` section
  for user-visible changes.
- Make sure `uv run pre-commit run --all-files` passes before opening the PR.

## Reporting bugs and security issues

- Functional bugs: open a [GitHub issue](https://github.com/mikhaillazko/fastbff/issues)
  with a minimal reproduction.
- Security vulnerabilities: **do not** open a public issue — follow
  [`SECURITY.md`](SECURITY.md) for private reporting.
