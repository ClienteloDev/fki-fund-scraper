# Extraction

Extraction reads values out of parsed documents and decides which of them may be delivered.
It never reads a document that was not attributed to the fund, and it never delivers a value
without the sentence it came from.

Source files: `field_extraction.py` (delivered fields), `extended_extraction.py` (extended
fields), `field_definitions.py` (vocabulary), `extraction_service.py` (orchestration),
`extended_persistence.py` (SQLite rows).

## Entry points

| Function | Field |
| --- | --- |
| `extract_investment_horizon` | `investment_horizon` |
| `extract_minimum_investment` | `minimum_investment` |
| `extract_target_return` | `target_return` |
| `extract_fees` | `fees` |
| `extract_aum` | `assets_under_management` |
| `extract_party` | `manager`, `administrator` |
| `extract_aum_history` | `aum_history` |
| `extract_annual_returns` | `annual_returns` |
| `extract_historical_values` | `historical_values` |
| `extract_fund_news` | `news` |

`extract_fund_fields` and `extract_extended_fields` call them; `extract_fund_data` in
`extraction_service.py` loads the documents, calls both and writes the result.

## Candidates

Every reader produces `Candidate` objects (`field_extraction.py`) or `SeriesCandidate`
subclasses (`extended_extraction.py`): the value, the raw text, the quote, the page number,
the source document, a score and the offset of the value inside the normalized document.

The score is built from a per-field document-type priority map — `HORIZON_DOCUMENT_PRIORITY`,
`MINIMUM_DOCUMENT_PRIORITY`, `TARGET_DOCUMENT_PRIORITY`, `FEE_DOCUMENT_PRIORITY`,
`AUM_DOCUMENT_PRIORITY`, `PARTY_DOCUMENT_PRIORITY`, `CAPITAL_DOCUMENT_PRIORITY`,
`RETURN_DOCUMENT_PRIORITY` — plus per-field bonuses. `confidence_from_score` maps it to
confidence: `>= 145` high, `>= 105` medium, otherwise low.

## Scope confirmation

`classify_source_scope` decides which entity a source actually describes, returning a
`SourceScope`: `exact_fund`, `subfund`, `share_class`, `manager`, `other_fund` or `generic`.
Only `ACCEPTED_SCOPES` — the first three — may be attributed to the requested fund. A
document that names some fund but not this one is rejected rather than ranked lower, and
`_unconfirmed_scope_result` explains which of the three failure modes applied
(`scope_mismatch`, `entity_not_matched`).

Scope also ranks: `SCOPE_RANKS` gives exact fund 3, subfund 2, share class 1. A share-class
value is marked `review_required` and never delivered as high confidence.

### What may prove identity

Exactly four things, and nothing else:

1. an official ISIN of this fund printed in the source (`official_isin_scope`);
2. the exact legal name, read where a fund name goes — immediately in front of `sicav`,
   `investiční fond` or `podfond` (`_named_fund_matches`);
3. the fund's own section of a page that also presents its neighbours (`fund_sections`,
   `_section_scope`);
4. an address whose path spells this fund out and no other — its panel or document folder
   on a shared hub (`url_identifies_fund`).

**The host of the address is not one of them.** `url_identity_text` drops it before the
identity text is built, because a manager, an administrator and a group serve every fund
they run from one host. Neither is the crawl assignment: which fund a page was reached for
is a property of the run, and on a shared host every fund of the house gets the same one.

The `scope` and `scope_accepted` columns that `document_metadata` writes for each parsed
document are inventory. Nothing in extraction reads them, and a consumer that treats them
as identity will misattribute.

Until online recovery batch 01, a fifth route existed: a count of how many of the fund's
distinctive words appeared anywhere in the title, the URL *including its host*, and the
first 5 000 characters. Half of them was enough. On `onecap.cz`, shared by two funds, the
host supplied `onecap` and the English marketing copy supplied `private`, `equity`, `real`,
`estate` and `infrastructure`, so one sentence about a strategy — on a page stating no
legal name, no ISIN and no IČO — became `target_return = 17 %` for both funds. A source
that proves none of the four now returns `generic` and is refused.

## Vocabulary

`field_definitions.py` holds the Czech and English wording each field is recognised by, and
the classifiers that turn wording into a typed decision: `PARTY_ROLE_LABELS`,
`CAPITAL_METRIC_LABELS`, `RETURN_SERIES_LABELS`, `HISTORICAL_VALUE_LABELS`,
`RETURN_TYPE_LABELS`, `SHARE_CLASS_PATTERN`, `NOT_PUBLISHED_MARKERS`,
`NEGOTIATED_FEE_MARKERS`, and helpers such as `share_class_code`, `classify_return_type`,
`classify_annualization`, `months_from_period` and `is_generic_company_name`.

Czech text is matched through `normalize_search_text` (accent folding, case folding,
whitespace collapse) and, where an offset into the original text is needed,
`fold_aligned`, which folds without moving any character.

## Assembling a result

`candidate_or_missing` (`field_extraction.py`) is the common tail for single-value fields:

1. classify the scope of every candidate; drop the ones outside `ACCEPTED_SCOPES`;
2. rank by scope, then score, then source id;
3. resolve conflicts among candidates that claim the same thing
   (see [conflict-resolution.md](conflict-resolution.md));
4. compute confidence and `review_required`, applying the AnyDoc rule when the source was
   read by the layout fallback;
5. build a `FieldResult` with `DataScope`, `Evidence` (source metadata, quote, page) and
   `ExtractionMetadata`, plus the losing candidates as `attempted_sources`.

`_series_result` (`extended_extraction.py`) is the equivalent for the four series fields. It
additionally resolves conflicts per observation, narrows and caps the series, validates the
assembled value, and refuses it outright when validation rejects it.

## Series semantics

- `aum_history` — one `CapitalObservation` per metric, date, share class and currency;
  `_capital_key` defines that identity and `_best_per_capital_key` orders and caps.
- `annual_returns` — one observation per year, series type and share class (`_return_key`).
  Year-to-date, rolling, cumulative, annualized and KID-scenario figures keep their own
  `ReturnSeriesType` and never enter the calendar-year series silently.
- `historical_values` — one series per value type, share class and currency
  (`_historical_key`); `_build_historical_collection` builds one `HistoricalValueSeries` per
  identity so two metrics can never share a list of observations.
- `news` — official fund or manager pages only, read from the stored HTML body; items that
  do not name the fund on a shared manager site are dropped.

## Persistence

`extended_persistence.persist_extended_fields` writes the extended fields into
`fund_parties`, `capital_observations`, `annual_returns`, `historical_values` and
`fund_news`. The delivered JSON remains the primary artefact; the tables exist for querying
and for offline comparison.

## Offline re-extraction

Because extraction reads only `cache/parsed` and the database, a change in the rules can be
measured without crawling:

```powershell
uv run python scripts/reextract_offline.py `
    --input data/input/funds.json `
    --database cache/regen.sqlite3 `
    --output data/output/funds.full.json
```

The processing database is opened read-only by this script, so the original run stays
intact. `--fund-id` limits it to individual funds.

## Limitations

- Extraction is regex- and table-driven; there is no language model in the delivered path.
  `grounding_packets.py` and `grounded_application.py` exist to prepare evidence packets for
  an external decision maker, but they are a separate, manual flow.
- Two readings of the same document that disagree are reported, not adjudicated by content
  plausibility — see [conflict-resolution.md](conflict-resolution.md).
- `news` never carries a page number, and most delivered items carry no publication date,
  which the audit reports as `news_without_date`.
