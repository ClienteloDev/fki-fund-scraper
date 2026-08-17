# 01 — Grounded path refuses SICAV funds their own legal name

Status: done
Spec: `../spec.md`
Type: bugfix
Blocked by: —

## Defect

`grounding_packets.py:234 FUND_NAME_NOISE_TOKENS` is missing `"zakladnim"`. The primary parser's
list at `field_extraction.py:798` has it, added with a comment recording the production failure:
the SICAV legal form *"s proměnným základním kapitálem"* appears in 29 canonical funds' registered
names, and while the token was absent those funds could not match their own legal name — their
homepage read as a foreign fund and every value on it was refused.

The two lists are otherwise token-for-token identical. `grounding_packets.py` does not import
`field_extraction.py`, so the fix never propagated.

Same input, same helper name, same stated purpose:

```
fund_identity_tokens("Nemomax investiční fond s proměnným základním kapitálem, a.s.")

field_extraction.py:3576      -> ("nemomax",)
grounding_packets.py:802      -> ("nemomax", "zakladnim")   # wrong
```

The grounded/AnyDoc review path still carries the bug the primary path fixed.

## Scope

One token, one file. **No refactor here** — consolidation is issue 02. Resist the urge to import
`field_extraction` from `grounding_packets` as a shortcut; 02 does it properly with a leaf module.

## Steps

1. Write `tests/test_fund_identity.py` first. Core case parametrized across **both** consumers —
   `field_extraction.fund_identity_tokens` and `grounding_packets._fund_identity_tokens` — asserting
   both return `("nemomax",)` for the SICAV name above.
2. Run it. Confirm it fails **only** on the grounded case, and fails with the extra `"zakladnim"`
   token rather than for any other reason. A test that passes here has not reproduced the bug.
3. Add `"zakladnim"` to `grounding_packets.py:234`, positioned as in `field_extraction.py:798`
   (between `"promennym"` and `"kapitalem"`). Copy the explanatory comment across — the next reader
   needs the reason, and 02 will fold both copies into one anyway.
4. Add the other-side case: `Český fond SICAV, a.s.` → `("cesky",)` in both consumers. This is the
   guard-rail against over-stripping and it matters later — see the spec's union counterexample.
5. Targeted run, then the full suite.

## Test data

Fund names verbatim from `data/input/funds.json`. Drive the grounded-path case from a real stored
parse in `cache/parsed` for a SICAV fund. If none can be located, record that in `## Comments`
below rather than substituting a synthetic fixture.

## Verification

```powershell
uv run pytest -q tests/test_fund_identity.py
uv run pytest -q tests/test_source_scope.py tests/test_grounding_packets.py
uv run pytest -q
uv run mypy
uv run ruff check src tests scripts
uv run ruff format --check src tests
```

No network. No `--force`. Nothing written to the production cache.

## Done when

- `tests/test_fund_identity.py` failed before the change for the stated reason, passes after
- both consumers return `("nemomax",)` and `("cesky",)`
- full pytest, mypy, ruff all pass
- `tests/test_source_scope.py:570` and `tests/test_grounding_packets.py:40` still pass unchanged

The offline 341-fund gate runs **once**, after 02 — the two commits are re-extracted together.

## Comments

### Test data: no real `cache/parsed` document was used

The issue asked for the grounded-path case to be driven by a real stored parse from
`cache/parsed`, recording it here if none could be located. It could not be used, and the reason is
structural rather than a lookup failure: `/cache/` is gitignored (`.gitignore:58`), so a test
reading it would pass only on a machine that had run the crawl and fail in CI and for every other
developer. The whole existing suite is self-contained on `tmp_path` — the `"cache/parsed/..."`
strings in `tests/test_field_extraction.py:71` and its siblings are recorded database column values,
not files anyone opens.

What is real and committed is the input that matters here: all four fund names in the test are
verbatim from `data/input/funds.json`, and the count in the source comment was re-verified —
exactly **29** canonical funds carry `s proměnným základním kapitálem` in their registered name.

### The defect at the grounded seam is inflation, not refusal

Worth correcting for whoever reads this next. The issue described the production failure as funds
being *refused* their own legal name, which is what happened on the `field_extraction` path. That is
not what the missing token does in `grounding_packets.py`, and the difference decides what the
regression test can assert.

`_fund_identity_tokens` feeds exactly one thing here: `_collect_field_snippets:536` counts how many
of the fund's tokens appear in a document's identity text, and `_snippet_score:641` turns that count
into `+10` each, capped at `+40`. There is no threshold and nothing is dropped for scoring zero. So
the extra token could never refuse a document — it could only *add* credit.

It adds it in the worst possible place. `zakladnim` appears in the identity text of a document about
*any* of the 29 SICAV funds, so a memorandum belonging to a different fund on a shared manager host
scored a fund-identity match against ours. That is an unearned increase in attribution
permissiveness — the exact class of change the spec's acceptance gate blocks on — arriving through
the ranking rather than through a verdict.

The regression test asserts that directly: a real other SICAV's memorandum
(`PRAGORENT …`) must score `fund_identity_matches == 0` for `Nemomax …`. Before the fix it scored
`1`, and the snippet's total score was `165` instead of `155`.

### Verification

```
uv run pytest -q tests/test_fund_identity.py     5 passed
uv run pytest -q                                 785 passed
uv run mypy                                      no issues in 126 source files
uv run ruff check src tests                      all checks passed
uv run ruff format --check src tests             126 files already formatted
```

`uv run ruff check src tests scripts` reports 66 errors, all in `scripts/recovery/` and all
pre-existing — `git diff HEAD -- scripts` is empty for this change. Left alone under the
"don't modify unrelated files to satisfy linting" rule; they belong to the tooling preserved in
`92087db`.

No network, no `--force`, nothing written to the production cache.

### Code review — what was fixed and what was left

Fixed in this commit: the parametrized tests carried a `consumer: str` parameter neither of them
read (`ids=` already supplies the label), and the grounded case called `datetime.now(UTC)` twice
where `build_grounding_packets` offers a `now=` parameter precisely so the clock can be pinned.
Both are now a single `RUN_TIME` constant, satisfying `.claude/rules/testing.md` "do not depend on
the current date" rather than merely inheriting the neighbouring test's precedent.

Left, and worth knowing about: the grounded-path test's fixture setup is a near-verbatim copy of
`tests/test_grounding_packets.py:43–158` — the same
`initialize_database` → `register_funds` → `upsert_source` → `ParsedDocument` →
`record_parsed_document` → `model_copy` sequence, about 115 lines differing only in fund name, URL,
sha256, title and page number. It is real duplication and it was cloned, not written.

De-duplicating it means introducing a shared fixture (there is no `tests/conftest.py`) and editing
at least one existing test module, and the same scaffolding recurs in `tests/test_field_extraction.py`
and `tests/test_target_return_and_fee_tiers.py` — so the honest scope is a test-infrastructure
change across several files, not a tidy-up. Folding that into a one-token bugfix would breach
`CLAUDE.md` "avoid unrelated refactoring" and make the commit hard to review. Recorded here as
follow-up work; it is not blocked by anything in this feature.
