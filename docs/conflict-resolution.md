# Conflict resolution

When two sources state different values for the same thing, the extraction preserves both,
compares them on a fixed ladder of factors, and prefers one only when it is clearly
stronger. Otherwise the disagreement survives into the output.

Source file: `conflict_resolution.py`. Consumers: `field_extraction.candidate_or_missing`
and `extended_extraction._series_result`. Report: `scripts/report_conflicts.py`.

## What counts as the same claim

Candidates contest one another only when they claim the same thing. The grouping key is
per field:

| Field | Conflict key |
| --- | --- |
| `investment_horizon` | the field itself |
| `minimum_investment` | `(kind, share_class)` |
| `target_return` | `(return_type, share_class, subfund)` |
| `fees` | the field itself, compared by `fee_collection_key` |
| `assets_under_management` | `(metric_type, as_of, currency)` |
| `aum_history` | `(metric, date, share class, currency)` — `_capital_key` |
| `annual_returns` | `(year, series_type, share_class)` — `_return_key` |
| `historical_values` | `(value_type, share_class, currency, date)` — `_historical_key` |
| `manager`, `administrator` | the role |

A different reporting date, share class, fee tier, return type or capital metric is a
*different claim*, not a disagreement, and is never reported as a conflict.

## Normalization first

Two values that read differently may be the same value. Before anything is compared:

- `number_key` rounds to six decimals;
- `money_key` rounds to two decimals and upper-cases the currency — two different currencies
  are never normalized into equality;
- `company_key` reduces a company name to its alphanumerics, so `a.s.`, `a. s.` and `a.s`
  agree;
- `fee_collection_key` compares a schedule by its charges and tiers, not by the wording of
  its basis or the order of its items.

A group whose members all agree after normalization is recorded as
`equivalent_after_normalization` and is not a conflict.

## The ladder

`compare_candidates(left, right, score_margin=...)` walks the factors in order; the first
one that separates the two decides, and a factor that cannot speak stays silent.

1. **`parser_provenance`** — a layout-fallback value never beats a primary-parser value from
   a source of equal or better authority and scope.
2. **`source_authority`** — `official_fund` > `fund_specific_manager` > `generic_manager` >
   `external_fallback` > `unknown`. `classify_authority` is only ever applied to a candidate
   whose entity the scope rules already confirmed, so a domain never proves belonging; it
   ranks sources that were already accepted.
3. **`scope_specificity`** — exact fund > subfund > share class.
4. **`document_type`** — the field's own document-priority map.
5. **`information_date`** — newer wins, but only when both dates are the *same kind*.
   `DateKind` is `as_of`, `reporting_period_end`, `effective_at` or `published_at`; a
   statute's effective day and a factsheet's as-of day are not comparable and decide
   nothing. Dates are read lazily by `DateReader` through `document_dates`.
6. **`evidence_completeness`** — a quote is the minimum; a page counts when the source is
   paginated, and a web page is not penalised for the page it cannot have.
7. **`extraction_score`** — decides only past `SCORE_DECISION_MARGIN` (25).

A candidate wins only by beating **every** rival. Beating the weakest of three while tying
with the strongest is not a decision.

## One document read twice

When every candidate in a group comes from the same `source_id`, this is not two sources
disagreeing but one document read two ways. Everything above `extraction_score` is identical
by construction, so the margin drops to `SAME_DOCUMENT_SCORE_MARGIN` (1) and any difference
in match quality decides. If the readings still tie, the group stays unresolved — which is
honest, because a wider crawl would return the same page and the same two readings.

## Outcomes

`ConflictOutcome` is `equivalent_after_normalization`, `auto_resolved` or `unresolved`.

- **Auto-resolved** — the winner is delivered as `found`; every losing rival is written into
  `attempted_sources` with `ReasonCode.CONFLICTING_VALUES`, its quote, and the factor that
  decided against it.
- **Unresolved, single value** — the field becomes `FieldStatus.CONFLICTING` and both sides
  are listed in `attempted_sources`.
- **Unresolved, one observation of a series** — the observation is left out, both readings
  are recorded, and the field is forced to `review_required`. Refusing a whole history
  because one of its years is contested would lose more than it protects. A series in which
  *every* point is contested becomes `_unresolved_series_result`.

## The ledger and the report

`ConflictLedger` collects a `ConflictRecord` per inspected group: the fund, the field, the
semantic key, the outcome, the reason, the deciding factor, the selected candidate, the
alternatives and every ranking factor. Each candidate view keeps value, source URL and
title, document type, quote, page, date and date kind, scope, authority, parser name and
score.

The ledger is internal. `FundOutput` and the delivery schema are unchanged.

```powershell
uv run python scripts/report_conflicts.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --report reports/conflicts.step8.json
```

`--limit N` or `--fund-id fund_...` for a subset, `--examples N` for console detail. The
script re-runs extraction offline and makes no requests. Expect a full-dataset run to take
tens of minutes.

## How the crawler uses this

`crawl_planning.conflict_is_between_sources` reads the report and sends a fund back out for
a deeper crawl only when the unresolved alternatives sit on **two different URLs**. A
conflict inside one document is never a reason to crawl more. See [crawler.md](crawler.md).

## Limitations

- The resolver compares *sources*, not plausibility. A fee schedule that resolves to `0 %`
  on extraction score is caught by the validation rule `zero_fee_not_supported_by_source`,
  not here.
- Party-name extraction can leak fragments (a role prefix, a date, a year) into a company
  name, which the resolver then reads as a genuine disagreement. Loosening `company_key`
  would hide the defect rather than fix it.
- Dates are re-derived from document text when `document_metadata` is empty, which costs
  time on a large run.
