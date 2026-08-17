# Fund identity tokens — shared seam for Tier A

Status: ready-for-agent
Branch: `feature/recovery-pipeline-v2`
Issues: `issues/01-zakladnim-grounded-path-regression.md`, `issues/02-tier-a-shared-identity-tokenizer.md`, `issues/03-avant-digit-token-asymmetry.md`

## Problem

"Which words of a fund's legal name identify *this* fund?" is answered independently in eleven
modules under six identifier names. Two of those answers have the same stated purpose, the same
algorithm and the same helper name — and have drifted.

`field_extraction.py:798 FUND_NAME_NOISE_TOKENS` contains `"zakladnim"`, added with a comment
recording a real production failure: the SICAV legal form *"s proměnným základním kapitálem"*
appears in the registered name of 29 canonical funds, and while the token was absent those funds
could not match their own legal name — on their own homepage the name read as a foreign fund and
every value on the page was refused.

`grounding_packets.py:234 FUND_NAME_NOISE_TOKENS` is token-for-token identical **except** that it
never received `"zakladnim"`. `grounding_packets.py` does not import `field_extraction.py` at all.

Verified consequence, same input string, same helper name, same stated purpose:

| Consumer | `fund_identity_tokens("Nemomax investiční fond s proměnným základním kapitálem, a.s.")` |
| --- | --- |
| `field_extraction.fund_identity_tokens:3576` | `("nemomax",)` |
| `grounding_packets._fund_identity_tokens:802` | `("nemomax", "zakladnim")` |

The grounded/AnyDoc review path therefore still carries the bug the primary parser fixed.

**Corrected during issue 01.** The two paths consume the tokens differently, so the same missing
token does different damage in each. On the primary path an unmatched legal name refuses the page.
On the grounded path there is no refusal — `_collect_field_snippets:536` only counts how many of the
fund's tokens appear in a document, and `_snippet_score:641` adds `+10` per match. So the extra
token can only *add* credit, and it adds it to documents belonging to any of the 29 SICAV funds:
another fund's memorandum on a shared manager host scored a fund-identity match against ours. The
grounded symptom is unearned attribution permissiveness, not refusal. See issue 01's `## Comments`.

## Why a global union is unsafe

The obvious remedy — one union of all eleven lists — is rejected. It breaks live funds.

Only `discovery_priority.py:246` and `site_crawler.py:41` treat `cesky`/`czech` as noise, with the
comment *"Words shared by every Czech fund name"*. That is correct for ranking crawl links and
fatal for establishing identity, because `data/input/funds.json` contains two distinct real funds
whose entire distinctive content is exactly those tokens:

| Fund (real, in the canonical 341) | Distinctive tokens today | Under a global union |
| --- | --- | --- |
| `Český fond SICAV, a.s.` | `("cesky",)` | `()` |
| `Czech Investment Fund SICAV, a.s.` | `("czech",)` | `()` |

Both collapse to an empty tuple, becoming indistinguishable from each other and from generic
boilerplate in every identity consumer. `Czech Investment Fund SICAV, a.s.` is served from
`avantfunds.cz`, so `avant._entity_key:641` — the adapter directly responsible for matching that
fund's catalog entry on that exact domain — would compute `""` instead of `"czech"`.

That is an increase in attribution permissiveness, which the acceptance gate below treats as
blocking. **Do not build a global union token list.**

Other union collisions found against the real 341-fund input, each independently sufficient to
reject the union:

- `invest` is noise only in `discovery_priority` / `site_crawler`; a union strips it from
  `AMBEAT INVEST SICAV, a.s.`, `SNP INVEST, investiční fond, a.s.`, `Ta Meri Invest SICAV, a.s.`,
  `r2p invest SICAV, a.s.`
- `investments` likewise, affecting `ZDR Investments SICAV a.s.` (`("zdr","investments")` →
  `("zdr",)`), `ZDR Investments Public SICAV a.s.`, `EXPANDIA Investments SICAV, a.s.`,
  `Sirius Investments`, `4 Gimel Investments SICAV, a.s.`
- `nemovitostni` / `realitni` are noise only in `amista` (it administers property funds); a union
  strips "real estate" from `FIDUROCK nemovitostní fond SICAV, a.s.`, `Trigea`, `Trikaya`,
  `Ambeat Care`, `Max Realitní Fond SICAV a.s.`, `Fio realitní fond SICAV, a.s.`
- `management` is noise only in `field_definitions` (a *company* list); a union strips it from
  `Robot Asset Management SICAV a.s.`, `Wine Management SICAV a.s.`, `Tatra Asset Management
  SICAV a.s.`, `LOAN MANAGEMENT investiční fond, a.s.`, and others

Not caused by unification and out of scope: `Jet 2/3/4 SICAV`, `JTFG FUND I/V` and `ČCE (A)/(B)`
collide today because their differentiating token is a single character already dropped by every
consumer's own minimum-length filter. Unifying lists neither causes nor worsens this.

## Tier A / B / C ownership

The eleven lists partition by *the question the consumer answers*, not by identifier name.

### Tier A — in scope now

Same question, same algorithm, proven drift.

| List | Consumer |
| --- | --- |
| `field_extraction.py:798 FUND_NAME_NOISE_TOKENS` | `fund_identity_tokens:3576`, plus direct reads at `iter_named_funds:3379` and `_named_fund_matches:3535` |
| `grounding_packets.py:234 FUND_NAME_NOISE_TOKENS` | `_fund_identity_tokens:802`, called from `_collect_field_snippets:517` |

Both fold via `html_discovery.normalize_search_text`, split on `[a-z0-9]+`, drop tokens shorter
than 2, dedup into a tuple. The implementations are identical; only the data drifted.

### Tier B — separate ticket, not this work

The `entity_key` family. All answer "reduce two names to a comparable match key", but each carries
legitimate local vocabulary.

`domain_manager_fallback.py:34`, `domain_adapters/amista.py:25`, `domain_adapters/bhs.py:25`,
`domain_adapters/porovnejfondy.py:29`, `domain_adapters/avant.py:60`, `domain_candidates.py:1105`.

Local extras that must survive any future consolidation: amista's `nemovitostni`/`realitni`;
domain_candidates' `czk`/`eur`/`sif`/`kvalifikovanych` (hostname guessing — these collide with no
fund in the 341-fund set).

### Tier C — must remain separate, permanently

Different question, different algorithm, different match shape.

| List | Why it must not converge |
| --- | --- |
| `discovery_priority.py:225` | Link/page priority scoring. Minimum length **3**, not 2. Treats `cesky`/`czech` as noise — correct here, fatal for identity. |
| `site_crawler.py:23` | Minimum length **3**. Matches **asymmetrically**: tokenizes the fund name only, then does a substring containment test against raw anchor text + URL (`site_crawler.py:162`). Every other consumer tokenizes both sides. |
| `field_definitions.py:223` | **Party/company** names (manager, administrator, depositary) via `is_generic_company_name:1552` → `clean_party_name:1476`. The only **DETECT** consumer: returns `bool` when *every* token is generic, and never strips. |

## Out of scope

Recorded here so a future reader does not re-derive them, and so nobody widens this change:

- **Tier B consolidation** — its own ticket, needs its own before/after evidence.
- **`avant._entity_tokens:647` has no `.isdigit()` filter**, unlike its four siblings
  (`domain_manager_fallback:748`, `amista:267`, `bhs:251`, `porovnejfondy:333`). A fund
  distinguished only by a numeral compares differently under avant. No test covers it.
  → `issues/03-avant-digit-token-asymmetry.md`, unstarted.
- **A twelfth list**: `document_identity.py:509 GENERIC_FUND_WORDS`, in a module commit `ca2d08e`
  just touched. Not counted in the eleven. Leave alone.
- **Three diacritics folders**: `html_discovery.normalize_search_text:617` (the only one that
  collapses whitespace), `field_definitions.fold_diacritics:1565`, `document_identity.fold_diacritics:528`.
  Unifying these changes normalization for every consumer at once — a far wider blast radius than
  the token lists, and one the offline diff could not attribute cleanly. Leave alone.
- **A false docstring**: `field_definitions.py:12-15` claims "All matching runs on text passed
  through `fundscraper.html_discovery.normalize_search_text`". That is untrue for
  `is_generic_company_name`/`clean_party_name`, which use the local `fold_diacritics`. Worth a
  one-line correction whenever someone next edits that file. Not now.
- **Scope resolution.** Identity stays separate from it — see below.

## Identity is not coupled to scope resolution

Checked, not assumed. `fund_identity_tokens:3576` is `str → tuple[str, ...]`. Its body references
only `normalize_search_text`, `re.findall`, `FUND_NAME_NOISE_TOKENS` and a length filter. It
touches **no** `SourceScope`, no verdict, no section index. Its two callers
(`classify_source_scope:3066`, `foreign_section_ratio:3243`) pass a plain `str`; everything
downstream receives an already-built token tuple as a parameter.

`SCOPE_MISMATCH_KEYWORDS:779` is a list of *sentence* fragments used only by
`classify_source_scope:3052`. It is a separate concern from a noise-*word* list and does not move.

**Zero lines of scope-resolution code move.** The only consequence is that `iter_named_funds:3379`
and `_named_fund_matches:3535`, which read the constant directly rather than through the tokenizer,
each gain one import line.

## Migration and import plan

### New module

`src/fundscraper/fund_identity.py` — a leaf holding roughly 52 lines lifted verbatim:

- `FUND_NAME_NOISE_TOKENS` (the `field_extraction.py:798` version, **including** `"zakladnim"` and
  its explanatory comment)
- `fund_identity_tokens(fund_name: str) -> tuple[str, ...]` (from `field_extraction.py:3576`)

Its only imports are `re` and `from fundscraper.html_discovery import normalize_search_text`.

### Why that placement is cycle-free

`html_discovery.py` is a leaf with respect to all eleven files — its own `fundscraper` imports are
just `normalization` and `output_models`. Ten of the eleven already import `normalize_search_text`
from it. The only import edges among the eleven are `field_extraction → field_definitions` and
`domain_manager_fallback → {domain_candidates, amista, avant}`; neither touches the new module.
`field_definitions.py` does not import `field_extraction.py` back — the edge is one-directional.

### Consumer edits

| File | Change |
| --- | --- |
| `field_extraction.py` | Delete `FUND_NAME_NOISE_TOKENS:798` and `fund_identity_tokens:3576`. Add `from fundscraper.fund_identity import FUND_NAME_NOISE_TOKENS, fund_identity_tokens`. `iter_named_funds:3379` and `_named_fund_matches:3535` keep reading the name unchanged. |
| `grounding_packets.py` | Delete `FUND_NAME_NOISE_TOKENS:234` and `_fund_identity_tokens:802`. Import the shared pair. Point `_collect_field_snippets:517` at `fund_identity_tokens`. |

Because `from … import fund_identity_tokens` re-binds the name into `field_extraction`'s namespace,
existing imports of the form `from fundscraper.field_extraction import fund_identity_tokens`
continue to work. **No existing test import needs editing.** Do not add a deprecation shim; there is
no external consumer to protect.

## Regression strategy

New file `tests/test_fund_identity.py`, created in issue 01 — before the module it is named for
exists — so that the drift itself is what is under test.

The core test is **parametrized across both consumers**: it calls
`field_extraction.fund_identity_tokens` and `grounding_packets._fund_identity_tokens` with the same
SICAV legal name and asserts both return `("nemomax",)`. Before the fix the grounded case fails
with `("nemomax", "zakladnim")`; after it, both pass.

That test must survive issue 02 with its **assertions** unchanged. In 01 it proves the two
implementations agree; in 02 it proves there is only one implementation. If an assertion needs
editing during 02, the consolidation changed behaviour and should be re-examined.

**Corrected during issue 01.** An earlier draft of this rule said the test must survive 02 wholly
unchanged, which is not achievable and would have sent the implementer looking for a fault that
isn't there. 02 deletes `grounding_packets._fund_identity_tokens` and adds no deprecation shim, so
the *import* in `tests/test_fund_identity.py` necessarily changes — that is the consolidation
working, not breaking. The rule binds the assertions, not the import line.

Case data comes from real sources per the project's testing rules: every fund name is taken verbatim
from `data/input/funds.json`.

**Settled during issue 01.** The grounded-path case is *not* driven by a stored parse from
`cache/parsed`. `/cache/` is gitignored (`.gitignore:58`), so such a test would pass only on a
machine that had run the crawl and fail in CI and for everyone else; the existing suite is
self-contained on `tmp_path` throughout. The realism that matters is carried by the fund names
instead — the document in the grounded case belongs to a real second SICAV fund
(`PRAGORENT …`), which is what makes the assertion meaningful.

Cover both sides of the rule: a name whose boilerplate must be stripped, and a neighbouring name
whose distinctive token must be kept. `Český fond SICAV, a.s.` → `("cesky",)` is the natural
guard-rail case, because it is the fund a careless union would erase.

### Existing tests that pin current list content

These assert exact tuple or string equality and must be re-verified, not assumed:

- `tests/test_source_scope.py:570 test_legal_form_boilerplate_is_not_an_identity_token` — pins
  `fund_identity_tokens(...) == ("nemomax",)`, i.e. requires `zakladnim` in the list
- `tests/test_source_scope.py:587 test_fund_recognises_its_own_name_on_its_own_homepage`, `:525`, `:602`, `:622`
- `tests/test_grounding_packets.py:40 test_builds_grounded_packet_for_unresolved_field`

Tier C tests must remain untouched and passing, which is the check that Tier C really did stay
separate: `tests/test_discovery_priority.py:142`, `tests/test_site_crawler.py:9`,
`tests/test_field_definitions.py:101`.

No test imports any of the eleven frozensets by name, so no test needs an import rewrite.

## Acceptance gate — offline, 341 funds

Network policy: **nothing is fetched.** The run reads `cache/regen.sqlite3` read-only and the stored
parses on disk. No `--force`.

Existing tooling already covers this; nothing new is built.

1. **Baseline** — `uv run python scripts/offline_baseline.py` against
   `data/output/funds.delivery.current-enriched.json` (341 funds, the current snapshot).
2. **Re-extract** — `uv run python scripts/offline_reextract.py --database cache/regen.sqlite3
   --output cache/<run>/extraction.jsonl --isin-register cache/isin-recovery/isin-hits.json
   --resume`. Budget roughly 15–25 s per fund; run it in the background.
3. **Delta** — `uv run python scripts/offline_delta.py --extraction … --authoritative
   data/output/funds.delivery.current-enriched.json --database cache/regen.sqlite3
   --isin-register cache/isin-recovery/isin-hits.json --output reports/<run>-delta.json`.

`offline_delta.py` attributes each candidate **by ablation** — it re-extracts with one change
switched off and credits the fix only if the value disappears — and its docstring already names this
exact case: *"the boilerplate token only matters to a fund whose registered name carries the
phrase."* SICAV gains from `zakladnim` are therefore separated mechanically, not by judgement.

### Pass criteria

- Gains **attributed by ablation to the boilerplate token** are expected. Report them separately
  with a fund count; they need no individual review.
- Every **other** newly appearing field is reviewed manually before acceptance.
- **Disappearing** fields do not block. They are listed in the report with fund and field.
- Any **unexpected increase in attribution permissiveness blocks acceptance** — in particular any
  value newly attributed to a fund on a manager-shared host, or any fund whose distinctive tokens
  shrink to `()`. Assert explicitly that `Český fond SICAV, a.s.` and
  `Czech Investment Fund SICAV, a.s.` still yield `("cesky",)` and `("czech",)`.
- Issue 02 alone should produce **no** field-level movement. It is a pure code move: the token data
  after 01 already matches what the shared module ships. Any delta from 02 is a defect in the move.

### Standard verification, both commits

```powershell
uv run pytest -q
uv run mypy
uv run ruff check src tests scripts
uv run ruff format --check src tests
```

## Commits

Two, in order, on `feature/recovery-pipeline-v2`. Do not commit until asked.

Keep `.claude/.agents/` and `.claude/skills-lock.json` out of both — untracked artifacts of plugin
setup, unrelated to this work.

## Comments
