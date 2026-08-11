# Testing rules

## Test-driven development

For bug fixes and non-trivial behavior changes:

1. reproduce the issue with a focused test
2. verify the test fails for the expected reason
3. implement the smallest safe change
4. run the targeted tests
5. run the full verification suite

A test that passes before the fix has not reproduced the bug.

## What to test

Prefer regression tests built from real cached documents and real observed failures over
synthetic fixtures.

Name the defect in the test name and, where the reason is not obvious, in a short docstring.

Cover both sides of a rule: the value that must be refused and the neighbouring value that
must keep passing.

For a threshold, test the boundary itself as well as the value past it.

## What not to test

Do not assert on incidental formatting, dictionary ordering or log text.

Do not write a test that only restates the implementation.

## Determinism

No network access in tests. Use `httpx.MockTransport` for HTTP and `tmp_path` for the
filesystem and SQLite.

Do not depend on the current date, on set or dict iteration order, or on the hash seed.

## Running

Targeted tests first:

```powershell
uv run pytest -q tests/test_<module>.py
```

Before declaring an implementation complete:

```powershell
uv run pytest -q
uv run mypy
uv run ruff check src tests scripts
```

When changing formatting-sensitive files also run:

```powershell
uv run ruff format --check src tests
```

## Definition of done

- targeted tests pass
- full pytest passes
- mypy passes
- Ruff passes
- no unintended network requests occurred
- remaining limitations are reported
