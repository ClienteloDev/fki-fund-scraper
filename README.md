# Fundscraper

Fundscraper collects publicly available information about Czech qualified investor funds
(*fondy kvalifikovaných investorů*) from the funds' own websites and official documents,
and delivers it as a validated, fully sourced JSON file.

The project favours correctness over coverage. A missing value is preferred to a plausible
but unsupported one, and every accepted value carries the document, the quoted sentence, the
page and the fund/subfund/share-class scope it came from.

## What it extracts

**Delivered fields** — `investment_horizon`, `minimum_investment`, `target_return`, `fees`,
`assets_under_management`.

**Extended fields** — `manager`, `administrator`, `aum_history`, `annual_returns`,
`historical_values`, `news`.

Each field is a `FieldResult` carrying a status (`pending`, `found`, `not_found`,
`ambiguous`, `conflicting`, `error`), the value, the evidence, the scope, an extraction
confidence, a `review_required` flag, and the sources that were attempted and rejected.
See [docs/data-model.md](docs/data-model.md).

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Network access only for crawling; every other step runs offline

```powershell
uv sync
uv run fundscraper --help
```

A `Dockerfile` and `compose.yaml` are present but describe the older single-pass MVP flow.
The commands in this README are the current, verified way to run the project.

## Canonical paths

| Purpose | Path |
| --- | --- |
| Canonical input | `data/input/funds.json` |
| Current full output (internal, auditable) | `data/output/funds.full.json` |
| Clean delivery output | `data/output/funds.delivery.json` |
| Processing database | `cache/regen.sqlite3` |
| HTTP response cache | `cache/http` |
| Parsed document cache | `cache/parsed` |
| Reports | `reports/` |

Other files under `data/input/` are historical samples and retry lists. Never use them as
the input of a full run. Nothing in the code depends on how many funds the input holds.

Input format:

```json
[
  { "name": "Example Fund SICAV, a.s.", "web": "https://example.cz" }
]
```

`web` may be `null` for a fund whose official website is not known; such a fund is reported
as unpublishable at export rather than given an invented address.

## Quick start

Validate the input and inspect the database without touching anything:

```powershell
uv run fundscraper validate-input data/input/funds.json
uv run fundscraper db-status --database cache/regen.sqlite3
uv run fundscraper validate-output data/output/funds.full.json
```

## Targeted offline work

Re-run extraction for one fund from the already parsed documents. No network access:

```powershell
uv run fundscraper extract-fund "TBGF SICAV a.s." `
    --database cache/regen.sqlite3 `
    --output data/output/funds.full.json
```

Explore the official sources of one fund without any fallback adapter (this does make
requests, but reuses `cache/http`):

```powershell
uv run fundscraper discover-official "TBGF SICAV a.s." --cache-directory cache/http
```

## Full offline re-extraction

Apply the current extraction, validation and conflict rules to every document already in
the cache. Nothing is downloaded and the processing database is only read:

```powershell
uv run python scripts/reextract_offline.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --output data/output/funds.full.json
```

Pass `--fund-id fund_...` to limit it to individual funds. Expect roughly 15–25 seconds per
fund, so run the whole dataset in the background.

## Two-pass online crawl

The current crawl strategy gives every fund a cheap first pass and sends only the funds with
missing, rejected or contested fields back out with a larger, field-aware budget.

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

Useful options: `--limit` / `--offset` / `--fund-id` to crawl a subset, `--skip-deep-pass`
to report the deep-pass selection without crawling it, `--deep-limit N` to cap the second
pass at the N highest-priority funds, `--progress-every N` for progress lines,
`--avant-fallback` `--amista-fallback` to allow the external adapters, `--anydoc-fallback`
to enable the optional layout parser. The canonical fund list is always registered in full,
so limiting a run to a sample never shrinks the database or the output file.

See [docs/crawler.md](docs/crawler.md) for budgets and selection rules.

## Delivery export

`funds.full.json` is the internal, auditable record: every value carries its document,
quote, page, scope, confidence and refused alternatives. The delivery file is derived from
it and carries only the business data — each field is a `status`, a `value` and optionally
the `source_url`.

```powershell
uv run fundscraper export-delivery `
    --input data/output/funds.full.json `
    --output data/output/funds.delivery.json `
    --audit reports/output-audit.step4.json
```

The audit is required, and it has to be the audit of the file being exported *by the rules in
force*: the export refuses any report whose `input_sha256` differs from the hash of its input
or whose `ruleset_version` differs from `AUDIT_RULESET_VERSION`. Any field the audit
calls suspicious, conflicting, rejected or missing is delivered as `not_found` with a null
value, so a doubted number never leaves as clean data.

`--unsafe-without-audit` exports on the extraction status alone. It exists for development
and says so on the console; the delivery it writes may carry values the audit would have
withheld. `--no-source-url` drops the document address as well. The command only reads the
internal file and refuses to write over it.

Only `found` and `not_found` appear in the delivery format; `ambiguous`, `conflicting`,
`error` and `pending` all become `not_found` with a null value. Nothing is invented.

## Output audit

Classifies every field of every fund as `valid`, `suspicious`, `conflicting`, `rejected` or
`missing`, with a stable reason code and a recommended action. Read-only:

```powershell
uv run python scripts/audit_enriched_output.py `
    --input data/output/funds.full.json `
    --report reports/output-audit.step4.json `
    --examples 10
```

See [docs/validation.md](docs/validation.md).

## Conflict report

Re-runs extraction offline and records every disagreement between sources: resolved
automatically, equivalent after normalization, or left unresolved and marked for review.

```powershell
uv run python scripts/report_conflicts.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --report reports/conflicts.step8.json
```

Add `--limit` or `--fund-id` for a quick subset. See
[docs/conflict-resolution.md](docs/conflict-resolution.md).

## Tests and checks

```powershell
uv run pytest -q
uv run mypy
uv run ruff check src tests scripts
uv run ruff format --check src tests
```

## Project structure

```text
src/fundscraper/       application package
  official_discovery   official-first source discovery (Step 5)
  crawl_planning       two-pass budgets and deep-pass selection (Step 7)
  two_pass_service     orchestration of the fast and deep passes
  crawl_service        page crawl and document download
  document_parser      PDF/HTML parsing, tables, page geometry
  document_*           classification, identity, dates, metadata
  field_extraction     delivered fields
  extended_extraction  extended fields
  extended_validation  the single validation rule vocabulary (Step 4)
  conflict_resolution  deterministic source comparison (Step 8)
  output_audit         the final audit over a delivered file
  delivery_export      the clean output for colleagues, API and frontend
  database             SQLite schema and repositories
  cli                  Typer command line
scripts/               offline analysis and reporting entry points
tests/                 pytest suite
docs/                  detailed documentation
```

## Documentation

- [architecture.md](docs/architecture.md) — modules and how they fit together
- [pipeline.md](docs/pipeline.md) — the end-to-end flow
- [discovery.md](docs/discovery.md) — official-first discovery, sitemaps, prioritization
- [crawler.md](docs/crawler.md) — the two-pass strategy and budgets
- [parsing.md](docs/parsing.md) — document parsing and the optional AnyDoc fallback
- [extraction.md](docs/extraction.md) — how each field is read and scoped
- [validation.md](docs/validation.md) — validation rules, statuses and the audit
- [conflict-resolution.md](docs/conflict-resolution.md) — how disagreeing sources are compared
- [data-model.md](docs/data-model.md) — output schema and database tables
- [operations.md](docs/operations.md) — runbook for setup, runs and recovery
