# Online-recovery tooling

The tooling batch 06 ran on, preserved verbatim from the session scratchpad. Only session paths
were removed; no logic was changed. These are **recovery tools, not production components** — the
fetch layer disables TLS verification, keeps no rate limiting and pretends to be a desktop browser,
three things `src/fundscraper/http_client.py` does properly and this must never teach it.

Companion tooling from the emergency work lives in `scripts/emergency/` and is imported by several
of these scripts (`em.py`, `orsl2.py`, `register_search.py`, `avanthub.py`, `amistahub.py`).

## The queue

### Formula

Unchanged from the v1 queue apart from one new multiplicative factor:

```
expected_raw = 1.00·HIGH + 0.60·MEDIUM + 0.15·LOW + 0.00·UNLIKELY   per missing field
expected     = expected_raw · route_quality · penalties · bonuses · AGE_PENALTY
score        = expected / expected_minutes
```

Ordering: `score_values_per_minute` desc, then `expected_recoverable_fields` desc, then
`fund_index` asc — deterministic and recomputable.

`route_quality`, `penalties`, `bonuses` and `expected_minutes` are read straight out of
`reports/ONLINE_RECOVERY_QUEUE.csv` (v1) and are **not** recomputed. The per-field HIGH/MEDIUM/LOW
ratings come from `reports/recovery-opportunities.csv`. § 3 of `reports/ONLINE_RECOVERY_HANDOFF.md`
documents the v1 factors.

### `age_penalty`

Field-aware rather than a blunt multiplier: it removes exactly the expected value of the four
fields that cannot exist without a closed **reported** accounting period, and leaves everything
else at ×1.

```
age_penalty = Σ(w(field) · age_mult(field, class)) / Σ w(field)      over the fund's missing fields
```

| age class | closed reported periods | `assets_under_management` | `aum_history` | `annual_returns` | `historical_values` |
|---|---|--:|--:|--:|--:|
| `NO_CLOSED_PERIOD` (established 2026) | 0 | ×0.15 | ×0 | ×0 | ×0 |
| `ONE_STUB_PERIOD` (established 2025) | 1, partial | ×0.60 | ×0 | ×0 | ×0.15 |
| `TWO_PERIODS` (established 2024) | 1 stub + 1 full | ×1.0 | ×0.60 | ×0.25 | ×0.70 |
| `MATURE` (2023 or earlier) | ≥ 2 | ×1 | ×1 | ×1 | ×1 |

The multipliers are calibrated on what the programme returned, not on theory: batch 05's five
2025-registered funds returned 0 `aum_history`, 0 `annual_returns`, 0 `historical_values` and 1
`assets_under_management` between them; batch 03's ASCAVIA and AVANT BESS returned statute fields
only; batch 04's GAMA and JTFG FUND II, both entered early in 2024, returned full financial sets.

A fund with no age evidence, or one whose remaining missing fields are not those four, keeps
`age_penalty = 1.0` and does not move.

Classification order in `rebuild_queue.py::classify`:

1. **first accounting period** if known — its *end* is the sharper signal, because a prolonged
   first period closes once, not twice (Accolade Assets was entered 20. 12. 2024 but its first
   period runs to 31. 12. 2025, so it is `ONE_STUB_PERIOD`);
2. else the earliest recorded **establishment date**;
3. else a recorded **establishment year**;
4. else the **IČO band**.

**Positive counter-evidence overrides everything**: if the database already holds an
`aum_history` / `historical_values` / `annual_returns` with observations in two or more distinct
years, the fund demonstrably has ≥ 2 closed periods and is forced to `MATURE`.

### Evidence sources

No source is acquired to build the penalty. `extract_ages.py` mines only what the programme already
holds:

| source | what is read |
|---|---|
| `data/output/emergency/*records.jsonl` and `*delta.json` | `reason` and `residual_gap_reason` sentences — the richest vein |
| `data/output/recovery-online/batch-0*-records.jsonl` / `-delta.json` | the same, per batch |
| `data/output/funds.delivery.current-enriched.json` | `evidence`, `condition`, `note`, `scope` text on applied values |
| `reports/online-recovery-batch-0*.md`, the two handoffs, `reports/emergency-stage-*.md` | narrative write-ups, matched by fund-name key |

Patterns: `vznikl(a/o) <date>`, `zapsán(a) [do …] <date>`, `Den zápisu: <date>`, `založen(a) <date>`,
`entered … <date>`, `první (účetní) období <date> – <date>`, plus year-only forms
(`fond zapsán v roce 2025`, `vznikly v únoru/březnu 2026`).

IČO bands are anchored on the funds for which both the IČO and the establishment date were already
known — JTPEG 19 466 340 → June 2023, JTFG FUND II 21 290 334 → February 2024, GLAMOUR 22 175 938 →
October 2024, ESG HOLDING 23 442 310 → June 2025, AGROCORE 24 106 020 → December 2025. **Only two
bands are used**, `20 900 000 – 22 500 000` (2024) and `22 500 000 – 24 000 000` (2025). Above
24 000 000 the current allocation sequence collides with the 2009–2014 block — Budějovická
24 261 386, INFOND 24 207 543 and TTP invest 24 141 224 are all old companies — so that band is
deliberately unused.

### How to rebuild `ONLINE_RECOVERY_QUEUE_v2`

```powershell
# 1. mine the age evidence  ->  reports/age-evidence.json
python scripts/recovery/extract_ages.py

# 2. rebuild the queue      ->  reports/ONLINE_RECOVERY_QUEUE_v2.csv / .json
python scripts/recovery/rebuild_queue.py
```

To reproduce the **exact** v2 files batch 06 worked from, pass the frozen evidence explicitly and
write somewhere harmless first:

```powershell
python scripts/recovery/rebuild_queue.py <out_dir> reports/age-evidence.batch-06.json
```

`rebuild_queue.py [out_dir] [age_evidence.json]` — both arguments optional, defaulting to
`reports/` and `reports/age-evidence.json`.

Inputs it reads: `reports/ONLINE_RECOVERY_QUEUE.csv`, `reports/recovery-opportunities.csv`,
`reports/missing-data-by-fund.csv`, `reports/cnb-match.json`,
`data/output/funds.delivery.current-enriched.json` (read-only, never written).

Frozen artefacts kept beside the queue so it stays reproducible after the scratchpad is gone:

| file | what it is |
|---|---|
| `reports/age-evidence.batch-06.json` | the evidence extraction batch 06 actually built v2 from — 79 funds |
| `reports/age-evidence.json` | currently a copy of the above, so a default rebuild reproduces v2 |
| `reports/age-evidence.refreshed-2026-08-16.json` | what `extract_ages.py` produces once the batch-06 write-up is on disk — 87 funds; see the limitations below |

## Known limitations

1. **The penalty is a floor, not a census.** It can only see the 79 funds for which the programme
   already holds age evidence. 200 of the 268 remaining queue rows have none, and 130 of those have
   no IČO either. Batch 06 met a 2025 registration it had classified `MATURE` — Energy financial
   group Fund, entered 7. 3. 2025 — and the register status check caught it in one lookup.
   **Reading the date of entry per fund before planning any financial field is still mandatory;
   this tool does not replace it.**
2. **The markdown scan bleeds across neighbouring rows.** `extract_ages.py` takes a 400-character
   window after every occurrence of a fund-name key. In a narrative write-up with tables that
   window spills into the next row. Re-running the extractor today, with
   `reports/online-recovery-batch-06.md` on disk, adds one true finding (Energy financial group
   Fund, 7. 3. 2025) and **three false ones** — Budějovická and INFOND both pick up Accolade
   Assets' `2024-12-20`, and BIDLI, Reticulum and STING pick up Convenio's `2010-10-21`. The false
   penalties cost Budějovická 56 ranks and INFOND 25. **Verify any newly penalised fund against the
   register before trusting a refreshed extraction**, or rebuild from the frozen
   `reports/age-evidence.batch-06.json`.
3. **`sum()` of floats is interpreter-sensitive.** CPython 3.12 uses compensated summation, so
   `expected_raw_slots` differs from 3.10 by one ulp on a handful of rows; that can flip a tie.
   Rebuilding under `uv run python` (3.12) instead of the plain `python` on PATH (3.10) moves **11
   of 326 rows by at most two ranks** and leaves the next-10 eligible list unchanged. The committed
   v2 files were built with **plain `python` 3.10.6**. The code was left exactly as it was rather
   than switched to `math.fsum`, because changing it would change the published ordering.
4. **`TODAY` is hard-coded** to `2026-08-16` in `rebuild_queue.py`. It is used only to decide
   whether a first accounting period ending in 2026 has closed yet. Update it when rebuilding much
   later.
5. **The processed/eligible lists are hard-coded** in `rebuild_queue.py` — v1 ranks 1–40 (batches
   02–05), the ten batch-06 funds, and the nine batch-01 funds that are closed. MTK Invest stays
   eligible because it is the one batch-01 fund explicitly classified `RETRY_WITH_OFFICIAL_ROUTE`.
   Add the next batch's ten funds to `batch06` (or a new set) after each batch.
6. **`precheck.py` and `compare_runs.py` carry batch 06's ten funds as a literal list.** Edit the
   `FUNDS` table for the next batch; that is the only per-batch configuration in either script.
7. **`assemble.py` asserts but does not merge.** It refuses to emit if any applied value targets a
   slot whose status is not `MISSING`, and it never writes to the database.

## What each file is

### Queue

| file | role |
|---|---|
| `extract_ages.py` | mines establishment dates / first accounting periods out of the existing corpus → `reports/age-evidence.json` |
| `rebuild_queue.py` | age classification, `age_penalty`, IČO anchors, scoring and ordering → `ONLINE_RECOVERY_QUEUE_v2.csv` / `.json` |

### Per-fund pre-flight and acquisition

| file | role |
|---|---|
| `b6.py` | the batch's fetch/read layer: wraps `scripts/emergency/em.py` so reads walk every cache the programme holds (17 emergency caches + every `cache/recovery-online/*/manual`) while writes land only in the batch workspace. `served_by()` answers *already cached or newly acquired*; `probe_quality()` is the text-layer check including the broken-font ratio |
| `precheck.py` | check 1 of the pre-flight list: register status, date of entry, historic names, board, liquidation and the Sbírka listin filing list, one `STARTS_WITH` search per fund with a `CONTAINS&jenPlatne=VSECHNY` fallback for renames |
| `deed.py` | fetches one Sbírka listin deed inside a single session and caches the body at download time — the `/ias/content/download?id=` URL expires and then returns a 1 037-byte error page that probes as a 0-page document |
| `avantnews.py` | the AVANT `oznameni` / `vyzvy` / `napsali_o_nas` announcement channel, filtered by fund, reading each item's JSON-LD `datePublished` |
| `cachecheck.py` | what `cache/regen.sqlite3` already holds for a fund — inventory only, never identity evidence |
| `g.py` | grep one document in a workspace, printing pages, characters, which cache served it and the text-layer verdict |
| `lines.py` | print a line range of a document — for reading a table the grep cut in half |

### Scraper pass and bookkeeping

| file | role |
|---|---|
| `runs.sh` | the isolated `fundscraper run-fund` diagnostic pass, one workspace per fund, logs to `cache/recovery-online/runs/` |
| `compare_runs.py` | what each run produced, and for which slots — only candidates for slots the database has **empty** count as a scraper gain |
| `assemble.py` | builds the batch delta and `records.jsonl` from the per-fund findings files, asserting at build time that every applied value targets a `MISSING` slot |

## Batch-06 inputs kept beside the tooling

So that `assemble.py` and `compare_runs.py` stay reproducible after the scratchpad is cleaned, the
batch's own inputs were copied into `cache/recovery-online/`:

| path | what it is |
|---|---|
| `findings/*.json` | the ten per-fund findings files `assemble.py` builds the delta from |
| `scraper-verdicts.json` | the four reviewed scraper candidates and the ten run diagnostics |
| `scraper-raw.json` | `compare_runs.py`'s raw output for the ten runs |
| `runs/*.log`, `runs/summary.txt` | the `fundscraper run-fund` logs |
| `precheck/precheck.json`, `precheck/uplny-*.txt` | the register pre-check and the ten úplné výpisy |

Re-running `python scripts/recovery/assemble.py` from these reproduces
`data/output/recovery-online/batch-06-delta.json` and `batch-06-records.jsonl` byte-for-byte.

## Conventions the tooling assumes

- One isolated workspace per fund: `cache/recovery-online/<batch>-<n>-<slug>/{http,parsed,manual,output,db.sqlite3}`.
- `cache/regen.sqlite3`, `cache/http`, `cache/parsed` and the `cache/emergency-*` caches are **read
  only**. `b6.use()` is what enforces that for manual fetches.
- The database `data/output/funds.delivery.current-enriched.json` is never written by anything here.
- No `--force`; nothing re-downloads a URL the cache chain already holds.
