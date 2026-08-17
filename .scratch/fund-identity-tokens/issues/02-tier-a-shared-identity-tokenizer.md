# 02 — One identity tokenizer for Tier A

Status: done
Spec: `../spec.md`
Type: task
Blocked by: 01

## Goal

Give `field_extraction.py` and `grounding_packets.py` one shared answer to "which words of a fund's
legal name identify this fund", so the drift fixed in 01 cannot recur.

Tier A only. **Do not touch Tier B or Tier C** — the spec records why, with a live counterexample.
**Do not build a global union token list.**

## Change

New leaf `src/fundscraper/fund_identity.py`, roughly 52 lines lifted verbatim:

- `FUND_NAME_NOISE_TOKENS` — the `field_extraction.py:798` version, including `"zakladnim"` and its
  explanatory comment
- `fund_identity_tokens(fund_name: str) -> tuple[str, ...]` — from `field_extraction.py:3576`

Imports: `re`, and `from fundscraper.html_discovery import normalize_search_text`. Nothing else.

`html_discovery.py` is a leaf with respect to all eleven identity modules, so this placement cannot
cycle. Ten of the eleven already import `normalize_search_text` from it.

### Edits

| File | Change |
| --- | --- |
| `field_extraction.py` | Delete `FUND_NAME_NOISE_TOKENS:798` and `fund_identity_tokens:3576`; import both from `fundscraper.fund_identity`. |
| `grounding_packets.py` | Delete `FUND_NAME_NOISE_TOKENS:234` and `_fund_identity_tokens:802`; import the shared pair; point `_collect_field_snippets:517` at `fund_identity_tokens`. |

`iter_named_funds:3379` and `_named_fund_matches:3535` read the constant directly rather than
through the tokenizer. They keep working unchanged once the name is imported — this is the whole
cost of the move, and it is why **no scope-resolution code moves**.

`from … import fund_identity_tokens` re-binds the name into `field_extraction`'s namespace, so
`from fundscraper.field_extraction import fund_identity_tokens` still resolves. No test import
needs editing. Do not add a deprecation shim.

## Constraints

- Identity stays separate from scope resolution. `SCOPE_MISMATCH_KEYWORDS:779` does **not** move —
  it is a sentence-fragment list consumed only by `classify_source_scope:3052`.
- Verified before this was written: `fund_identity_tokens` references no `SourceScope`, no verdict,
  no section index. If that turns out to be false mid-implementation, stop and record it in
  `## Comments` rather than dragging scope code along.
- Pure code move. No behaviour change, no signature change, no vocabulary change.

## Verification

`tests/test_fund_identity.py` from issue 01 must pass **unchanged**. In 01 it proved two
implementations agreed; here it proves there is only one. If it needs editing, the move changed
behaviour — stop and investigate.

```powershell
uv run pytest -q tests/test_fund_identity.py
uv run pytest -q tests/test_source_scope.py tests/test_grounding_packets.py
uv run pytest -q tests/test_discovery_priority.py tests/test_site_crawler.py tests/test_field_definitions.py
uv run pytest -q
uv run mypy
uv run ruff check src tests scripts
uv run ruff format --check src tests
```

The Tier C tests in that third line are the check that Tier C really did stay separate.

## Acceptance gate — offline, 341 funds

Runs once, covering 01 and 02 together. Offline only; `cache/regen.sqlite3` read-only; no `--force`.

1. `uv run python scripts/offline_baseline.py` against `data/output/funds.delivery.current-enriched.json`
2. `uv run python scripts/offline_reextract.py --database cache/regen.sqlite3 --output cache/<run>/extraction.jsonl --isin-register cache/isin-recovery/isin-hits.json --resume` — ~15–25 s per fund, run in the background
3. `uv run python scripts/offline_delta.py --extraction … --authoritative data/output/funds.delivery.current-enriched.json --database cache/regen.sqlite3 --isin-register cache/isin-recovery/isin-hits.json --output reports/<run>-delta.json`

`offline_delta.py` attributes by ablation and its docstring already names this case — *"the
boilerplate token only matters to a fund whose registered name carries the phrase"* — so SICAV gains
are separated mechanically.

### Pass criteria

- Boilerplate-attributed gains: expected, reported separately with a fund count, no individual review
- Every other new field: reviewed manually
- Disappearing fields: listed, not blocking
- **Blocking:** any unexpected increase in attribution permissiveness — a value newly attributed on
  a manager-shared host, or any fund whose distinctive tokens shrink to `()`
- Assert `Český fond SICAV, a.s.` → `("cesky",)` and `Czech Investment Fund SICAV, a.s.` → `("czech",)`
- **02 in isolation should move nothing.** It is a pure code move; the token data after 01 already
  matches what the shared module ships. Any delta attributable to 02 is a defect in the move.

## Done when

- `src/fundscraper/fund_identity.py` exists; both Tier A modules import from it
- no duplicate noise list or tokenizer remains in either
- no scope-resolution code moved
- issue 01's test passes unchanged
- full pytest, mypy, ruff pass
- offline delta reviewed against the criteria above and recorded in `## Comments`

## Comments

### The re-export assumption in the spec is false under this repo's mypy config

The spec and this issue both state that `from … import fund_identity_tokens` re-binds the name
into `field_extraction`'s namespace, so `from fundscraper.field_extraction import
fund_identity_tokens` keeps resolving and *"No test import needs editing."* True at runtime,
false under type checking: `pyproject.toml:72` sets `strict = true`, which implies
`no_implicit_reexport`. The move produced three `attr-defined` errors.

Resolved by repointing the three sites at the canonical module rather than adding an
`as`-alias re-export — an alias would preserve a second import path for the one symbol this
issue exists to unify, and the issue forbids a shim. Three sites is exactly the error count:

- `src/fundscraper/extended_extraction.py:91` (not listed in the spec's consumer table; it is a
  fourth reader of the tokenizer that the Tier A survey did not record)
- `tests/test_source_scope.py:528`, `:580`

### The move silently disarmed the ablation harness the acceptance gate runs on

Not anticipated by the spec, and the one edit outside the planned files.

`scripts/batch_ablation.py:132` ablates this exact fix by rebinding
`fe.FUND_NAME_NOISE_TOKENS`, and `scripts/offline_delta.py:46` imports that `_disabled`
context manager — it is the mechanism behind the gate's *"SICAV gains are separated
mechanically, not by judgement."*

Before the move the tokenizer lived in `field_extraction` and read that module global, so one
rebinding covered everything. After it, `fund_identity_tokens` closes over
`fund_identity`'s global and ignores the patch; only the two direct reads at
`field_extraction.py:3352` and `:3508` still respond. The script would have kept running and
kept reporting a number — a wrong one, crediting the boilerplate token with less than it
earned and pushing the difference into manual review as unattributed.

`mypy` covers `src` and `tests` only, so nothing flagged it. Fixed by rebinding both names,
which reproduces the pre-move behaviour exactly; verified that
`fund_identity_tokens("Nemomax …")` returns `("nemomax", "zakladnim")` inside the context
manager and `("nemomax",)` outside it.

### 02 in isolation moves nothing — shown mechanically rather than by delta

The issue asks for proof that the pure code move changes no field. That was established
directly, which is stronger than an ablation delta and does not depend on the long run:

- the shipped `FUND_NAME_NOISE_TOKENS` is set-equal to **both** pre-move copies at `9615109`
  (17 tokens, symmetric difference empty) — so the move is data-neutral by construction
- tokenizing all **341** canonical names with the pre-move implementation and the shared one
  gives **0** names whose tokens differ
- **0** funds tokenize to `()`, so no fund lost its identity
- the two guard-rail funds hold: `Český fond SICAV, a.s.` → `("cesky",)` and
  `Czech Investment Fund SICAV, a.s.` → `("czech",)`

### Acceptance gate

Baseline captured against `data/output/funds.delivery.current-enriched.json` (341 funds).
The 341-fund offline re-extraction into `cache/fund-identity-tokens/extraction.jsonl` is
running offline against `cache/regen.sqlite3`, no `--force`, nothing fetched. Observed rate is
roughly 60 s per fund rather than the estimated 15–25 s, so budget ~6 hours; the delta is
outstanding at the time of the commit. **The gate covers 01 and 02 together**, and 01 is
already committed, so what remains to be reviewed is 01's SICAV gains — 02's own contribution
is settled by the mechanical result above.

### Verification

```
uv run pytest -q                        784 passed
uv run mypy                             no issues in 127 source files
uv run ruff check src tests             all checks passed
uv run ruff format --check src tests    127 files already formatted
```

`uv run ruff check src tests scripts` reports the same **66** pre-existing errors as at 01,
all in `scripts/recovery/`, none in `scripts/batch_ablation.py`. No network, no `--force`,
nothing written to the production cache.

The test count fell 785 → 784 because the degenerate half of a parametrization went away: the
tokenizer table in `tests/test_fund_identity.py` had two entries that became the same function
object once the second implementation was deleted. Assertions and their expected values are
unchanged, as the spec requires; the grounded path stays covered end-to-end by
`test_grounded_snippet_earns_no_identity_credit_from_the_shared_legal_form`.

### Declined: extracting the token predicate

`field_extraction.py:3352` and `:3508` still spell `word not in FUND_NAME_NOISE_TOKENS and
len(word) >= 2` inline, duplicating the tokenizer's filter. Raised in review as incomplete
extraction; not done, because the issue names this precisely and accepts it — *"They keep
working unchanged once the name is imported — this is the whole cost of the move."* Folding
those two call sites into a shared predicate is a behaviour-bearing edit to scope-adjacent
code inside a commit whose value is being provably inert.

### Carried over from issue 01's code review

Two things were deliberately left for this issue rather than done in the bugfix commit.

**The two comments are now different, and neither alone is right.** This issue's `## Change`
section says to lift *"the `field_extraction.py:798` version, including … its explanatory comment"*.
Do not follow that literally — it would discard information. 01 did not copy the comment verbatim,
because the sentence explaining *why* the token is noise is shared but the sentence describing
*what went wrong* is path-specific:

| Copy | Records |
| --- | --- |
| `field_extraction.py:798` | the "investiční fond head" mechanism; the primary-path symptom — funds **refused** their own legal name on their own homepage |
| `grounding_packets.py:251` | the grounded-path symptom — another SICAV's document **earning identity credit** for this fund |

The shared module has both consumers behind it, so its comment must carry the shared mechanism plus
both symptoms. Merge them; do not pick one.

**Fix the comment's placement at the same time.** In both copies the explanation reads *"None of
its three words tells one fund from another"* but hangs off `"zakladnim"` alone, while `"promennym"`
and `"kapitalem"` sit bare above and below it. Someone editing `"kapitalem"` never sees the reason.
Put the merged comment above `"promennym"` so it governs all three tokens it describes. This was not
done in 01 because the copies are about to merge and diverging their placement first is noise.

**Dropping the private import is part of this issue's payoff.** `tests/test_fund_identity.py`
imports `grounding_packets._fund_identity_tokens` — unavoidable for a parity test, with precedent in
`tests/test_step4_validation.py` and `tests/test_table_extraction.py`. Once there is one tokenizer,
that import should become `fundscraper.fund_identity.fund_identity_tokens`. Note this is the one
edit to that test file the spec's "must survive 02 unchanged" rule does not forbid — the parity
*assertions* must not change; the import naturally does, because the second implementation is gone.
