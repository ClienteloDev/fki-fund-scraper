# Data model

Two models matter: the delivered JSON (`output_models.py`) and the processing database
(`database.py`). The JSON is the artefact; the database is the index that lets a later run
start from what an earlier one already did.

## Fund identity

`output_service.stable_fund_identifier(name=..., web=...)` derives a deterministic
`fund_id` of the form `fund_<16 hex digits>` from the raw input name and website. A fund
without a known website still gets a stable id, so it can be reported and backfilled later.
The same identifier is used in the output, the database and every report.

## Output model

`FundOutput` is a strict pydantic model (`extra="forbid"`, whitespace-stripped,
validate-on-assignment):

```text
fund_id, name, web, identity, processing
investment_horizon        FieldResult[InvestmentHorizonValue]
minimum_investment        FieldResult[MinimumInvestmentValue]
target_return             FieldResult[TargetReturnValue]
fees                      FieldResult[FeeCollection]
assets_under_management   FieldResult[AssetsUnderManagementValue]
manager, administrator    FieldResult[FundParty]
aum_history               FieldResult[AumHistory]
annual_returns            FieldResult[AnnualReturnHistory]
historical_values         FieldResult[HistoricalValueCollection]
news                      FieldResult[FundNewsCollection]
```

The six extended fields default to `pending`, so an output written before they existed still
loads unchanged.

### FieldResult

```text
status              FieldStatus: pending | found | not_found | ambiguous | conflicting | error
value               the typed value, or null
raw_value           the text the value was read from
scope               DataScope: type, fund_name, subfund_name, share_class_name, isin
source              Evidence: SourceMetadata + quote + page + section
extraction          ExtractionMetadata: method, confidence, review_required
reason              MissingReason: ReasonCode + detail
attempted_sources   list[SourceAttempt]: url, retrieved_at, outcome, document_type, detail
```

A model validator enforces the contract: a `found` result must carry a value, a
`raw_value`, source evidence and extraction metadata, and must not carry a reason; a
`pending` result must carry nothing; every other status must carry a reason and no value.

`SourceMetadata` holds the URL, `DocumentType`, retrieval timestamp, title, publication and
effective dates and an optional SHA-256. `Evidence.page` is 1-based and is `null` for
non-paginated sources.

### Enumerations

- `FieldStatus` — `pending`, `found`, `not_found`, `ambiguous`, `conflicting`, `error`
- `Confidence` — `high`, `medium`, `low`
- `ExtractionMethod` — `regex`, `grounded`, `table`, `html_selector`, `llm`, `hybrid`, `manual`
- `ScopeType` — `fund`, `subfund`, `share_class`, `manager`, `unknown`
- `DocumentType` — `priips_kid`, `statute`, `subfund_statute`, `memorandum`,
  `annual_report`, `half_year_report`, `financial_statements`, `factsheet`, `infoletter`,
  `marketing_page`, `register`, `prospectus`, `price_list`, `investor_notice`, `other`
- `ReasonCode` — 24 codes for why a field has no value, from `not_publicly_disclosed` and
  `scope_mismatch` to `scanned_document_ocr_failed`
- `SourceKind`, `PartyRole`, `EntityMatchStatus`, `ProcessingStatus`

### Value types

- `InvestmentHorizonValue` — `recommended_years` plus `HorizonKind`
  (`exact`, `minimum`, `range`, `textual`) and optional bounds and wording.
- `MinimumInvestmentValue` — amount, ISO currency, `MinimumInvestmentKind`, optional share
  class, `ValueOrigin` (`explicit` / `inferred`) and, when inferred, an
  `InferenceReference` naming the legal basis, jurisdiction and effective date. The model
  itself refuses an inferred value without a basis.
- `TargetReturnValue` — an exact value or a range, `ReturnType`
  (`expected`, `target`, `guaranteed_minimum`, `preferred`, `hurdle`, `range`, `other`,
  `not_published`), `Annualization`, period, share class and subfund.
- `FeeCollection` / `FeeItem` / `FeeTier` — per fee type a rate or a fixed amount with
  currency, an optional min/max band, `FeeFrequency`, a `maximum` flag, a negotiable flag,
  free-text basis and condition, and tiers keyed by `FeeTierBasis`
  (`holding_period`, `investment_amount`, `share_class`, `distributor`, `other`).
- `AssetsUnderManagementValue` / `CapitalObservation` — amount, currency, `AumMetricType`
  and `as_of`. `FUND_LEVEL_AUM_METRICS` names the metrics that may populate the delivered
  assets field; `registered_capital`, `statutory_minimum_capital` and `manager_aum` are
  stored under their own metric and never as the assets of the fund.
- `AnnualReturnObservation` — year, percentage and `ReturnSeriesType`
  (`calendar_year`, `year_to_date`, `rolling_12m`, `cumulative`, `annualized_multi_year`,
  `kid_scenario`), plus share class and currency.
- `HistoricalValueSeries` — `HistoricalValueType`
  (`nav_per_share`, `investment_share_value`, `fund_net_assets`, `fund_capital`, `aum`),
  currency, `SeriesFrequency`, share class and observations. A validator refuses duplicate
  dates and observations whose currency differs from the series.
- `FundParty` — role, name, legal name, eight-digit IČO, website.
- `FundNewsItem` — title, URL, source domain, `NewsSourceType`, publication date, summary
  and a relation confidence.

### JSON Schema

Three schema files exist and they are not interchangeable:

| File | Contents |
| --- | --- |
| `schemas/extended-output.schema.json` | current — all sixteen top-level properties including the six extended fields |
| `schemas/delivery-output.schema.json` | the reduced external delivery shape |
| `docs/funds-output.schema.json` | **stale** — predates the extended fields; kept only as a historical artefact and no longer written to |

`generate-schema` writes the extended schema by default. Regenerate it after any change to
`output_models.py`:

```powershell
uv run fundscraper generate-schema
uv run fundscraper validate-output data/output/funds.full.json
```

`validate-output` validates against the model itself, not against a schema file.

## Delivery model

`delivery_export.py` derives a second, much smaller file from the internal output. It is a
different contract, meant for colleagues, an API or a frontend, and it is produced by
`export-delivery` — never written by the pipeline.

Each fund is `name`, `web` and the eleven `DELIVERY_FIELDS`. Each field is:

```json
{ "status": "found", "value": { ... }, "source_url": "https://..." }
```

or

```json
{ "status": "not_found", "value": null }
```

`source_url` is the one piece of provenance that crosses over, and `--no-source-url` drops
it. Only `found` and `not_found` exist; `ambiguous`, `conflicting`, `error` and `pending`
all become `not_found`.

What each value may contain is an **allowlist** per structure (`_FEE_ITEM_KEYS`,
`_CAPITAL_OBSERVATION_KEYS`, and so on), so a field added to `output_models.py` later stays
out of the delivery until somebody decides it belongs. Deliberately excluded: `fund_id`,
`identity`, `raw_value`, `scope`, `extraction`, `attempted_sources`, `reason`, quotes,
pages, sections, hashes, retrieval timestamps, `relation_confidence`, and the free-text
`basis` and `details` a fee keeps when the wording could not be normalized.

The dated series keep their observations as structured rows so a chart can still be drawn:
`aum_history.observations`, `annual_returns.observations` and
`historical_values.series[].observations`. Fee items keep their type, rate, fixed amount,
currency, frequency, maximum flag, min/max band, negotiable flag, condition and their full
`tiers`.

An audit report is required, and it must be the audit *of this file, by the current rules*.
The export checks four things and fails closed on any of them:

| Requirement | Refused when |
| --- | --- |
| `input_sha256` equals the SHA-256 of the input | the report describes other bytes |
| `ruleset_version` equals `AUDIT_RULESET_VERSION` | the report predates or postdates the rules in force |
| every finding's `fund_id` exists in the input | the report was edited or assembled by hand |
| `summary.funds` equals the number of funds | the same |

Sharing fund identifiers proves nothing — every run audits the same funds — so overlap is
never accepted as evidence. A report written before the version stamp existed carries no
`ruleset_version` and is refused as legacy.

Any field the audit calls `suspicious`, `conflicting`, `rejected` or `missing` is withheld as
`not_found`; a field it says nothing about is valid and may be delivered. The withholding is at whole-field level — a series with one doubted observation
is withheld entirely rather than trimmed. `--unsafe-without-audit` skips the audit and is
for development only.

Three delivery-shaped schemas now exist; see the JSON Schema table above.
`schemas/delivery-output.schema.json` describes an **older** five-field contract produced by
`scripts/export_delivery_output.py`, which carries a `reason` object and `retrieved_at` and
is not the format described here.

## Database

SQLite, `SCHEMA_VERSION = 5`, created and migrated additively by `initialize_database`.
Every table uses `CREATE TABLE IF NOT EXISTS`, and the applied version is recorded in
`schema_migrations`.

| Table | Holds |
| --- | --- |
| `schema_migrations` | version, applied_at |
| `funds` | one row per canonical input fund: id, name, web, canonical URL, status |
| `sources` | every discovered URL: type, content type, title, dates, local body path |
| `attempts` | one row per stage attempt per fund, with error code and message |
| `parsed_documents` | one row per parsed source: format, parser name, pages, characters, scanned flag, text path |
| `document_metadata` | classification, identity and dates of a parsed document |
| `discovery_log` | every inspected link: method, score, scope decision, accepted, rejection reason, run id |
| `fund_parties` | manager, administrator, depositary, auditor |
| `capital_observations` | dated fund-level capital figures |
| `annual_returns` | per-year performance |
| `historical_values` | dated value series |
| `fund_news` | news items |

`ParsedDocumentRecord` is the row extraction consumes; its `text_path` points into
`cache/parsed` and `local_path` into `cache/http`.

```powershell
uv run fundscraper db-status --database cache/regen.sqlite3
uv run fundscraper validate-db cache/regen.sqlite3
```

`db-status` takes the database as the `--database` option; `validate-db`, `validate-input`
and `validate-output` take their path as a positional argument.

## Compatibility rules

- Migrations are additive. A new table or a defaulted column is fine; dropping or retyping a
  column is not, without explicit approval.
- New output fields default to `pending` so older files still validate.
- Reason codes and validation codes keep their delivered strings, so reports from different
  runs can be compared.
- Nothing in the code depends on how many funds the input holds.
