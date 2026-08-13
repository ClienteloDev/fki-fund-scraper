# Validation and audit

Validation answers one question: is this value wrong in a way that being well-formed cannot
reveal? A series of two different metrics validates against the schema, a statutory minimum
capital is a perfectly good amount, and a news item about a neighbouring fund is a perfectly
good article. Only a rule that knows what the value is supposed to mean can refuse them.

Source files: `extended_validation.py` (the rules), `output_audit.py` (the audit over a
delivered file), `scripts/audit_enriched_output.py` (the entry point).

There is exactly one validation vocabulary. Extraction calls it to refuse a defective value;
the audit calls the same functions to explain a delivered one, so a defect caught before
delivery and one caught after it carry the same reason code.

## Severity

`ValidationSeverity` has three levels:

| Severity | Meaning | Effect at extraction | Audit status |
| --- | --- | --- | --- |
| `review` | unusual or unsupported | value kept, `review_required` | `suspicious` |
| `conflict` | two sources disagree, neither can be preferred | value refused | `conflicting` |
| `reject` | the value means something other than the field | value refused | `rejected` |

`rejects(findings)` is true for `reject` and `conflict`. Nothing is deleted from a delivered
file: a suspicious or rejected value keeps its value, source, quote, page, scope and
confidence, and the audit adds the reason code beside it.

## Statuses

`FieldStatus` in the output: `pending`, `found`, `not_found`, `ambiguous`, `conflicting`,
`error`.

`AuditStatus` in the audit report: `valid`, `suspicious`, `conflicting`, `rejected`,
`missing`. `missing` simply means the field was not `found`; it is not a defect.

## Reason codes

`ValidationCode` is a `StrEnum` of stable machine-readable codes, grouped by what they are
about: provenance (`missing_source`, `missing_evidence`, `missing_page_reference`,
`evidence_does_not_support_value`, `scope_mismatch`, `third_party_source`,
`source_shared_across_funds`, `source_does_not_name_the_fund`), shape
(`missing_value_component`, `missing_currency`, `unsupported_currency`, `malformed_value`),
and one group per field. The full list is the enum itself; every code in it is also a key of
`_VALIDATION_ACTIONS` in `output_audit.py`, which supplies the recommended action.

Codes that were delivered before Step 4 kept their original strings, so an old report and a
new one can be compared line by line.

### Attribution rules

Four codes exist because a value can be well-formed, correctly converted and still describe
something other than the fund it was written under:

- `capital_of_another_company` — the evidence attaches the amount to a company the fund
  holds rather than to the fund. It only fires when the amount is findable in the quote, a
  different company stands between the start of the quote and that amount, and neither the
  fund's own name nor a plain word for the fund stands any closer. A hard reject.
- `party_name_contains_a_date` — a manager or administrator stored with a leading date or
  year, captured together with the statute line that names the company.
- `unknown_annualization_for_annual_rate` — a target return delivered in a `_percent_pa`
  property that no source ever called annual.
- `zero_fee_of_another_fee_type` — a zero read from the row of a different fee in a cost
  table.

### Current assets against history

`assets_under_management` is what the fund holds **now**; `aum_history` is every dated
observation it published. They are read from the same documents and are easy to confuse, and
one delivered output confused them — a 2023 net asset value stood as the current figure while
the fund's own history already held a 2025 one.

`_aum_superseded_by_history` (a cross-field rule) reports a current figure that one of the
fund's own fund-level observations has outgrown. It compares dated evidence against dated
evidence, so nothing depends on today's date or on how old a value is allowed to be: a figure
is refused for being **superseded**, never for being old. A fund whose only figure is from
2019 keeps it, and the five-year `stale_as_of_date` rule speaks for that case instead.

Nothing is ever removed from `aum_history`. Reporting a superseded current figure says
nothing about the observation it lost to, and both stay in the series.

### Wording rules

Four codes exist because the number was read correctly and the words around it were not:

- `horizon_bound_lost` — the source states a floor ("min. 3 roky", "5 let a vice"), a span
  ("v rozmezi 3 - 5 let") or a ceiling, and the value stores a single exact figure.
- `capital_metric_contradicts_evidence` — the evidence names a different line of the
  statement than the value claims. Fund capital, net assets, a net asset value and equity are
  not interchangeable, and equal amounts do not merge them.
- `benchmark_return_as_fixed_rate` — the source states a rate over a reference
  (`2TR + 1 %`, `PRIBOR + X`, `inflace + X`) and the value stores the spread alone. A hard
  reject: the spread is not a weaker version of the promise but a different one.
- `newer_aum_observation_exists` — above.

Magnitude never refuses a fee on its own. An extreme rate is delivered when the source line
names that fee type and states the condition it applies under, and withheld when the number
belongs to a clause about who receives the fee (`percentage_describes_income_share`) or does
not appear in the evidence at all (`value_not_present_in_evidence`).

## Field specifications

`FIELD_SPECIFICATIONS` states, per field, what a value is allowed to be: value kind, unit,
accepted currencies, plausible range, whether an as-of date is required, whether source,
evidence and a page are required, the accepted `ScopeType`s, and the explicit `hard_reject`
and `review_only` code lists. The audit publishes this table in its report under
`field_specifications`, so a reader sees the rule a value was measured against.

Example — `assets_under_management`: money, CZK/EUR/USD, between 1e6 and 5e11, as-of date
required, hard rejects `manager_aum_as_fund_aum`, `statutory_capital_as_aum` and
`capital_of_another_company`.

The same magnitude ladder is applied to the dated series, not only to the single figure: a
fund-level observation below the per-share bound is `per_share_value_as_aum`, one below the
fund bound is `implausibly_small_aum`, and one above the sector bound is
`implausibly_large_aum`. `aum_history` and `historical_values` are narrowed the same way the
single figure is, so an annual report published in thousands is named
`thousands_unit_not_applied` rather than left as a bare magnitude complaint.

## Traceability

`validate_provenance(ValueProvenance)` checks that a found value can be traced back:
a source URL, a retrieval date, a quote, a page when the source is a PDF
(`is_paginated_source`), a scope the field accepts, and — for numeric fields — that at least
one of the value's numbers actually appears in the quote. `quote_supports_number` tries
every unit a Czech document writes amounts in, so `179 564 tis. Kč` supports 179 564 000 and
`3,645 mld. Kč` supports 3 645 000 000.

None of these findings refuses a value. What they take away is the right to be believed
without opening the source.

When a file stores no evidence at all — the reduced delivery schema has no place for a quote
or a page — the evidence rules are skipped and a schema note says so, because reporting every
field as unevidenced would describe the schema rather than the data.

## Cross-field rules

`validate_cross_fields(FundRecordView)` checks relations that only a whole record can see:

- delivered assets against the fund's own capital series for the same metric, date and
  currency (`conflicting_values`);
- delivered assets equal to a NAV-per-share or investment-share value
  (`per_share_value_as_aum`, a hard reject);
- a target return equal to a reported calendar-year result
  (`historical_return_as_target_return`);
- the manager and the administrator carrying the same name under different registration
  numbers. The same company acting in both roles is normal and is *not* reported.

## Ruleset version

`AUDIT_RULESET_VERSION` in `extended_validation.py` names the rules currently in force, and
every generated report carries it beside `schema_version` and `input_sha256`. The two
versions answer different questions: `schema_version` describes the shape of the report,
`ruleset_version` describes the judgement in it.

**Bump `AUDIT_RULESET_VERSION` whenever a rule is added, removed or changed in a way that
alters what an audit reports.** A digest cannot notice this on its own — the audited file can
stay byte-identical while what counts as a defect changes underneath it. One report of
`funds.after-fast.json` withheld 138 fields and the next report of the very same bytes
withheld 194, and delivery had no way to tell them apart.

## The audit

`audit_enriched_output(records, ...)` classifies every audited field of every fund and
returns an `AuditReport`: summary counts by status, field, reason, severity, source type and
scope, a `weakest_fields` ranking by share of delivered values that are doubted, the field
specifications, representative examples, and the full finding list. `write_audit_report`
writes it atomically.

The audit is read-only. It re-hashes the input file (`input_sha256`) so a report can be
matched to the exact file it describes, and it never modifies the audited output.

```powershell
uv run python scripts/audit_enriched_output.py `
    --input data/output/funds.full.json `
    --report reports/output-audit.step4.json `
    --examples 10
```

`SourceType` classifies where a flagged value came from relative to the fund:
`official_website`, `manager_or_administrator`, `third_party`, `none`.

## Limitations

- The audit reads a delivered file and cannot see the parser that produced a value, so the
  AnyDoc guarantees are enforced at extraction time and verified by tests rather than
  re-checked from JSON.
- Thresholds were calibrated against the distribution of the current full output. They are
  deliberately permissive: an unusual but possible value is marked for review rather than
  rejected.
- `news_without_date` fires for any fund whose news items lack publication dates, which is
  most of them; it is a real weakness of the delivery, but it dominates the suspicious count.
