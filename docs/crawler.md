# Crawler and the two-pass strategy

Crawling every fund as deeply as the hardest one costs a great deal and buys almost nothing:
most funds publish their key information document, statute and a report within a few pages
of the landing page. The two-pass strategy gives every fund a cheap first look and spends
the large budget only where the first pass left a question open.

Source files: `crawl_planning.py` (the decisions), `two_pass_service.py` (the orchestration),
`crawl_service.py` and `site_crawler.py` (the crawl itself), `http_client.py` (fetching).

## Budgets

`CrawlBudget` in `crawl_planning.py`:

| | official pages | official docs | crawl pages | depth | docs | doc concurrency | stop when sufficient |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `FAST_BUDGET` | 8 | 25 | 10 | 1 | 12 | 4 | yes |
| `DEEP_BUDGET` | 40 | 120 | 45 | 3 | 40 | 4 | no |

`stop_when_sufficient` lets the official discovery stop the moment it holds a document from
every required group; the deep pass exists precisely to look past the obvious documents, so
it never stops early.

`run_fund_pipeline(budget=...)` applies a budget to both the official stage and the crawler.
Passing no budget keeps the historical defaults (`max_pages=25`, `max_depth=2`,
`max_documents=20`, official 18/60).

## Selecting the deep pass

`select_deep_pass_funds(output_records, audit_findings, conflict_records,
discovery_outcomes)` reads four inputs, each answering a different question:

- the **delivered output** — which fields have no answer: status `pending`, `not_found`,
  `ambiguous`, `conflicting` or `error` (`UNANSWERED_STATUSES`);
- the **Step 4 audit** — which delivered answers are wrong: `rejected` or `conflicting`, and
  `suspicious` only when the reason is one a better source could fix
  (`SOURCE_FIXABLE_REASONS`: `missing_source`, `missing_evidence`, `third_party_source`,
  `source_does_not_name_the_fund`, `source_shared_across_funds`, `stale_as_of_date`,
  `manager_level_scope`, `scope_mismatch`, `only_manager_level_data`). "The amount is
  implausibly small" says the number was misread, and re-crawling will not change it;
- the **Step 8 conflict report** — which fields two *different sources* disagree about.
  A conflict whose alternatives share one URL is skipped;
- the **discovery outcome of the first pass** (`DiscoveryOutcome`) — whether anything is
  left to find at all: `sufficient` (every required group found), `exhausted` (the walk ran
  out of pages rather than budget) and `missing_document_groups`.

`TRIGGER_FIELDS` is the ten fields that may trigger a pass. **`news` is deliberately absent**:
an announcement page nobody publishes is not a reason to spend forty page fetches.

### Evidence gating

A field with no answer is only a crawling problem while there is somewhere left to look.
`_unanswered_trigger` applies three rules:

- a value that was **found but not confirmed** (`ambiguous`, `conflicting`, `error`) always
  counts — a better source can still settle it (`VALUE_UNCONFIRMED`);
- an **absent priority or supporting field** counts unless the site was both explored to the
  end and found to publish everything expected; on such a site the field is absent because
  the fund does not state it (`FIELD_UNANSWERED`);
- an **absent historical series** counts only when the `reporting_document` group was never
  reached (`SERIES_SOURCES_UNREACHED`). Across the current data set the dated series are
  found for a small minority of funds, so their simple absence describes the market rather
  than a gap in the crawl. Treating it as a gap selected almost every fund and recovered
  almost nothing.

### Priority

`PRIORITY_FIELDS` (`minimum_investment`, `target_return`, `fees`,
`assets_under_management`) weigh 10 each, `SUPPORTING_FIELDS` (`investment_horizon`,
`manager`, `administrator`) 3, and `HISTORICAL_SERIES_FIELDS` 1. `DeepPassPlan.priority`
sums them and the selection is returned strongest first, so `--deep-limit N` keeps the funds
whose missing fields matter most.

Each selected fund gets a `DeepPassPlan` with its `reasons` (field, `DeepPassTrigger`,
detail), its `priority` and its `wanted_document_types`.

## Field-aware deep crawling

`FIELD_DOCUMENT_TYPES` maps each trigger field to the documents that answer it:

| Fields | Wanted types |
| --- | --- |
| `investment_horizon`, `minimum_investment`, `target_return` | KID, statute, subfund statute, memorandum (+ factsheet) |
| `fees` | KID, statute, subfund statute, price list, memorandum |
| `assets_under_management`, `aum_history` | annual report, financial statements, half-year report, factsheet |
| `annual_returns` | annual report, factsheet, KID, infoletter |
| `historical_values` | factsheet, infoletter, annual report, financial statements |
| `manager`, `administrator` | statute, subfund statute, KID, annual report |

The plan's union of those types is passed to `discover_official_sources` as
`wanted_document_types`, which raises their link score by `WANTED_DOCUMENT_TYPE_BONUS` (60)
and adds them to the sufficiency requirement. **Any one** of the wanted types satisfies the
request — a field is answered by any of its documents, not all of them — and the unmet case
is reported as a single `wanted:a|b|c` entry.

## One authoritative copy

`select_authoritative_documents` runs before download, on the merged official and adapter
document list:

- `SINGLE_REVISION_TYPES` — KID, statute, subfund statute, memorandum, prospectus, price
  list — collapse to the newest revision, because only the one in force is wanted;
- every other type keeps one copy per year, so an annual report of 2022 and one of 2024 both
  survive while two copies of 2024 do not.

The newest year wins, then score plus document-type rank. The dropped copies are counted as
`superseded_documents` in the pipeline result and the report.

## Cache and dataset safety

- Both passes share `cache/http` and `cache/parsed`; the second pass re-downloads nothing
  that the first already fetched. `HttpFetcher.cache_hits` and `.cache_misses` are reported.
- `run_two_pass` takes `canonical_funds` and `selected_funds` separately. It calls
  `initialize_database`, `register_funds` and `synchronize_output_file` with the **canonical**
  list before crawling, so limiting a run with `--limit`, `--offset` or `--fund-id` can never
  shrink the database or the output file to the sample.
- `--force` bypasses the HTTP cache. Use it only for a specific reason.

## Concurrency and politeness

`HttpFetcher` enforces three limits at once: a global semaphore (`HTTP_CONCURRENCY`), a
per-domain semaphore (`HTTP_PER_DOMAIN_CONCURRENCY`, default 2) and an `AsyncLimiter` rate
limit (`HTTP_REQUESTS_PER_SECOND`). Fund-level concurrency is the CLI `--concurrency`
option; document downloads within a fund use `document_concurrency` from the budget.

Retries use tenacity with exponential jitter between `HTTP_RETRY_MIN_WAIT_SECONDS` and
`HTTP_RETRY_MAX_WAIT_SECONDS`; every HTTP failure leaves the client as a `FetchError` so one
bad address cannot kill a run.

## Running it

```powershell
uv run fundscraper two-pass `
    --input data/input/funds.json `
    --database cache/fundscraper.sqlite3 `
    --output data/output/funds.full.json `
    --report reports/step7-two-pass.json `
    --audit reports/output-audit.step4.json `
    --conflicts reports/conflicts.step8.json `
    --cache-directory cache/http `
    --parsed-directory cache/parsed `
    --concurrency 6
```

Options: `--limit`, `--offset`, `--fund-id` (repeatable), `--skip-deep-pass`,
`--deep-limit`, `--progress-every`, `--force`, `--avant-fallback`, `--amista-fallback`,
`--porovnejfondy-fallback`, `--anydoc-fallback`.

`--deep-limit N` crawls at most N funds in the deep pass, highest priority first.
`--progress-every N` prints one progress line every N funds:

```text
FAST 120/341 | fund=Example SICAV a.s. | pages=8 | docs=12 | elapsed=1h04m
DEEP 18/74 | fund=Example SICAV a.s. | targets=annual_report,factsheet
```

`--skip-deep-pass` still selects the funds that would be crawled again and reports them
under `deep_pass_selection`, but crawls none of them: `deep_pass.funds` is zero and no
second request is made. It is the cheap way to see what a deep pass would cost before
paying for it.

## The report

`reports/step7-two-pass.json` contains: canonical fund count, HTTP cache hits and misses,
per-pass counts (funds, pages visited, documents discovered, downloaded and parsed,
superseded copies skipped, failures, fund-seconds) with a per-fund breakdown including
`official_sources_sufficient` and `official_missing_document_groups`, the deep-pass selection
with fields, triggers and wanted document types, the fields the deep pass recovered, and the
failures per fund.

## Expected misses, warnings and failures

Discovery guesses `/robots.txt` and three conventional sitemap addresses for every site,
because one of them hands over the whole document list for a single request. Most sites
publish none, and `run_diagnostics.classify_fetch_failure` files the resulting 404 as an
`expected_miss` rather than a failure. A full first pass reported 874 "failures", of which
474 were those guesses and exactly one was a `MemoryError`.

| Level | What it means |
| --- | --- |
| `expected_miss` | a guessed optional address answered 403/404/410 |
| `warning` | a document-level problem: a linked file that 404s, one 5xx, a document that will not parse |
| `failure` | an unexpected error, a parser crash, or repeated 5xx from one host (`escalate_repeated_server_errors`) |

The counts appear per pass in the report (`expected_misses`, `warnings`, `real_failures`)
and per fund under `diagnostics`. `warnings` on `OfficialDiscoveryResult` keeps only what
deserves attention; nothing is discarded, and every diagnostic stays in `diagnostics`.

Sitemap addresses are also kept out of the document path entirely: they are excluded from
`navigation_urls` and skipped by the crawler when it records a page as a source. A full run
parsed 1 079 sitemap files as if they were fund documents — 11 per cent of everything it
parsed, one of them 140 000 characters and re-parsed once per fund of a shared manager
domain.

## Where the time goes

`StageTimings` on `FundPipelineResult` records `discovery`, `crawl`, `parse` and `extract`
seconds per fund. The report carries per-pass `stage_seconds`, the ten `slowest_funds` with
their breakdown, and per-fund `stage_seconds` in `funds_detail`.

## Limitations

- The deep pass is only as good as the reports it is given. Without `--audit` and
  `--conflicts` it selects on delivered field status alone.
- Recovery is not guaranteed: a fund whose site fails TLS validation, serves no documents or
  genuinely publishes no reports gains nothing from a larger budget, and the report records
  that plainly.
- Budgets are global constants, not per-domain. A very large manager site can still exhaust
  the deep budget before reaching a deep archive.
