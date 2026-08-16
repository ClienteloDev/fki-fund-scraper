# Operations runbook

Every command below has been executed against this repository. Commands are shown for
PowerShell; the backtick is the line continuation character.

Paths follow the canonical layout: input `data/input/funds.json`, output
`data/output/funds.full.json`, database `cache/regen.sqlite3`, caches `cache/http` and
`cache/parsed`, reports `reports/`.

## 1. Setup

```powershell
uv sync
uv run fundscraper --help
```

Copy `.env.example` to `.env` to override HTTP behaviour. The settings that are actually
read by `config.HttpSettings.from_environment` are:

```text
HTTP_TIMEOUT_SECONDS          default 30
HTTP_MAX_RETRIES              default 3
HTTP_CONCURRENCY              default 8
HTTP_PER_DOMAIN_CONCURRENCY   default 2
HTTP_REQUESTS_PER_SECOND      default 4
HTTP_MAX_RESPONSE_BYTES       default 50000000
HTTP_RETRY_MIN_WAIT_SECONDS   default 0.5
HTTP_RETRY_MAX_WAIT_SECONDS   default 5
HTTP_USER_AGENT               default fundscraper/0.1
```

The optional AnyDoc parser is an extra and is not installed by `uv sync`:

```powershell
uv sync --extra anydoc
```

## 2. Targeted tests

Run the tests for what you changed first:

```powershell
uv run pytest -q tests/test_field_extraction.py
uv run pytest -q tests/test_extended_validation.py tests/test_step4_validation.py
uv run pytest -q tests/test_conflict_resolution.py
uv run pytest -q tests/test_crawl_planning.py
uv run pytest -q tests/test_official_discovery.py tests/test_discovery_priority.py
```

## 3. Full verification

```powershell
uv run pytest -q
uv run mypy
uv run ruff check src tests scripts
uv run ruff format --check src tests
```

All four must pass before an implementation is considered done.

## 4. Database and output backup

Take a copy before any run that writes. `backups/` already holds dated snapshots.

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force "backups/$stamp" | Out-Null
Copy-Item cache/regen.sqlite3 "backups/$stamp/regen.sqlite3"
Copy-Item data/output/funds.full.json "backups/$stamp/funds.full.json"
```

If the database is in WAL mode, copy `-wal` and `-shm` alongside it, or checkpoint first.
Never delete or recreate `cache/regen.sqlite3`, `cache/http` or `cache/parsed` as part of
routine work.

## 5. Health checks

```powershell
uv run fundscraper validate-input data/input/funds.json
uv run fundscraper db-status --database cache/regen.sqlite3
uv run fundscraper validate-db cache/regen.sqlite3
uv run fundscraper validate-output data/output/funds.full.json
```

## 6. Offline extraction

No network access; the processing database is only read.

One fund:

```powershell
uv run python scripts/reextract_offline.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --output data/output/funds.full.json `
    --fund-id fund_dca0e5027a51aae2
```

Whole dataset — roughly 15–25 seconds per fund, so run it detached and watch the log:

```powershell
uv run python scripts/reextract_offline.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --output data/output/funds.full.json
```

The CLI equivalent for a single fund, writing straight into the output file:

```powershell
uv run fundscraper extract-fund "TBGF SICAV a.s." `
    --database cache/regen.sqlite3 `
    --output data/output/funds.full.json
```

## 7. Output audit

Read-only. Classifies every field and records the SHA-256 of the file it audited.

```powershell
uv run python scripts/audit_enriched_output.py `
    --input data/output/funds.full.json `
    --report reports/output-audit.step4.json `
    --examples 10
```

## 8. Conflict report

Re-runs extraction offline and records every disagreement between sources.

```powershell
uv run python scripts/report_conflicts.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --report reports/conflicts.step8.json
```

Quick subset while developing:

```powershell
uv run python scripts/report_conflicts.py --limit 5 --examples 5 `
    --report reports/conflicts.sample.json
```

A full-dataset run takes tens of minutes. Run it detached.

## 9. Full online two-pass run

Refresh the two selection inputs first, so the deep pass is chosen from current facts:

```powershell
uv run python scripts/audit_enriched_output.py --input data/output/funds.full.json `
    --report reports/output-audit.step4.json --examples 0
uv run python scripts/report_conflicts.py --input data/input/funds.json `
    --database cache/regen.sqlite3 --report reports/conflicts.step8.json --examples 0
```

Selection preview — the deep pass is selected and reported but not crawled, so you can read
which funds it would take and which document types it would look for before paying for it:

```powershell
uv run fundscraper two-pass `
    --input data/input/funds.json `
    --database cache/fundscraper.sqlite3 `
    --output data/output/funds.full.json `
    --report reports/step7-two-pass-selection.json `
    --audit reports/output-audit.step4.json `
    --conflicts reports/conflicts.step8.json `
    --skip-deep-pass `
    --concurrency 6
```

Full run:

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

Add `--avant-fallback --amista-fallback` to allow the external adapters when official
discovery falls short. Add `--anydoc-fallback` only if you want the optional layout parser;
its values stay `review_required` and below high confidence.

`--deep-limit N` caps the second pass at the N highest-priority funds, which is how a run
stays affordable when the selection is large. `--progress-every N` sets how often a progress
line is printed (default every 10 funds).

The end-of-run summary reports the runtime, both passes, how many funds were selected versus
executed, expected sitemap misses against real failures, cache hits and misses, the five
slowest funds with their stage breakdown, and the top deep-pass trigger reasons.

Run it detached and expect hours. Test a handful of funds first:

```powershell
uv run fundscraper two-pass --limit 5 `
    --database cache/step7-sample.sqlite3 `
    --output data/output/funds.step7-sample.json `
    --report reports/step7-sample.json
```

Even with `--limit`, the canonical fund list is registered in full, so the sample database
still describes the whole dataset.

## 10. Post-run extraction and validation

```powershell
uv run fundscraper validate-output data/output/funds.full.json
uv run python scripts/audit_enriched_output.py --input data/output/funds.full.json `
    --report reports/output-audit.step4.json --examples 20
uv run python scripts/report_conflicts.py --input data/input/funds.json `
    --database cache/fundscraper.sqlite3 --report reports/conflicts.step8.json
uv run fundscraper db-status --database cache/fundscraper.sqlite3
```

Compare the audit before and after: `summary.by_status`, `summary.weakest_fields` and
`summary.by_reason` are the three that answer "did this run help".

## 11. Delivery export

Derive the clean file colleagues and the frontend consume. The internal output is only read.

```powershell
uv run fundscraper export-delivery `
    --input data/output/funds.full.json `
    --output data/output/funds.delivery.json `
    --audit reports/output-audit.step4.json
```

The audit is not optional, and it must be the audit of the exact file being exported. Without
`--audit` the command writes nothing and exits 1. With one, the export hashes its input and
compares the digest against the report's `input_sha256`; a report of any other file is
refused, however many funds the two have in common. Run the audit against this output file
first — an audit applied to the wrong file would withhold nothing and deliver every doubted
value as clean.

The digest alone cannot see a report made from *this* file by older rules, so the report also
carries `ruleset_version` and the export requires it to equal `AUDIT_RULESET_VERSION`. A
report written before that stamp existed is refused as legacy. Re-run the audit after
changing validation, not only after changing the output — and bump the constant when you do,
or the stale report will still be accepted.

`--unsafe-without-audit` exports on the extraction status alone, for development only:

```powershell
uv run fundscraper export-delivery `
    --input data/output/funds.full.json `
    --output data/output/funds.delivery.json `
    --unsafe-without-audit
```

The command refuses to write over its own input, so it cannot damage `funds.full.json`.

## 12. Recovery and safety notes

**A run was interrupted.** Nothing is lost. `cache/http` and `cache/parsed` keep everything
already fetched and parsed, and a rerun skips them. Re-running the same command resumes in
effect, at the cost of re-deciding what to crawl.

**The output file looks wrong.** Restore it from `backups/` and re-run the offline
extraction; the delivered JSON is derived from the database and the parsed cache, so it can
always be rebuilt without crawling.

**A fund failed.** Its processing status becomes `failed` and the message is appended to the
fund's `processing.warnings`. Look at the `attempts` table for the stage, then rerun the
single fund with `run-fund` or the individual stage command.

**The run reports hundreds of failures.** Check the split first: the summary separates
expected sitemap misses from warnings and real failures. Discovery guesses `/robots.txt` and
three sitemap addresses for every site, and most sites publish none of them. Only the
`real failures` number is worth acting on.

**A site blocks or throttles.** Lower `HTTP_REQUESTS_PER_SECOND` and
`HTTP_PER_DOMAIN_CONCURRENCY` rather than the global concurrency. `robots.txt` is honoured
by default.

**Never** register a subset into the production database: always pass the canonical
`data/input/funds.json` and use `--limit`, `--offset` or `--fund-id` to restrict what is
crawled. **Never** use `--force` without a specific reason; it bypasses the HTTP cache and
re-downloads everything.

**Long runs on Windows.** Long-running commands are best started detached, writing to a log
file, and polled — a foreground shell may time out before a full-dataset run finishes.

## 13. Legacy entry points

`scripts/run_mvp_final.py`, `scripts/run-mvp-final.ps1`, `scripts/run-docker-mvp.ps1`,
`Dockerfile` and `compose.yaml` implement the older single-pass MVP flow
(official → AVANT → AMISTA) and write `data/output/funds.enriched.json`. They still run, but
the two-pass crawl supersedes them and the canonical output is now
`data/output/funds.full.json`.
