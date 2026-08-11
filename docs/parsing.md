# Parsing

Parsing turns a downloaded byte stream into text that keeps enough structure for extraction
to quote it, place it on a page and read it out of a table.

Source files: `document_parser.py`, `table_extraction.py`, `document_classification.py`,
`document_identity.py`, `document_dates.py`, `document_metadata.py`, `document_service.py`,
`anydoc_parser.py`, `anydoc_fallback.py`.

## Formats and parsers

`DocumentFormat` covers `pdf`, `html`, `xhtml`, `xml` and `text`. PDFs are read with
pymupdf, HTML with selectolax (`parser_name="selectolax"`), plain text directly
(`parser_name="plain_text"`). A PDF parse records which strategies it used in its
`parser_name`, which is what later lets the conflict resolver tell a primary parse from a
fallback one.

`ParsedDocument` holds `pages` (`ParsedPage`), each with its page number, text, character
count, `TextBlock` geometry and detected `DocumentTable`s. `full_text` joins the pages;
`iter_tables()` yields every table with its page number. Parsed documents are stored as JSON
under `cache/parsed` and reloaded by `load_parsed_document`.

## Tables

`table_extraction.py` reads values out of a detected grid rather than out of the flattened
text: `iter_labelled_rows` pairs a row label with its cells, `iter_table_values` and
`leading_percentage` turn a row into a typed value. A table candidate scores higher than the
same figure found in prose (`TABLE_SCORE_BONUS` in `extended_extraction.py`), because a row
carries its own label and period.

## What is known about a document

Three classifiers run before any value is read, and their results are stored in
`document_metadata` by `document_metadata.store_document_facts`:

- **`document_classification.py`** — the official type (`DocumentType`), a score, whether the
  classification was ambiguous, the runner-up, and the keywords that decided.
- **`document_identity.py`** — which fund the document belongs to: scope, confidence,
  accepted flag, rejection reason, subfund name, share class, matched IČO and ISIN. A
  document that is not accepted is not read for values.
- **`document_dates.py`** — `published_at`, `effective_at`, `reporting_period_start`,
  `reporting_period_end` and `as_of`, each with its `DateOrigin`
  (`document_text`, `document_metadata`, `file_name`, `derived`). The head and the tail of a
  document are searched, because a statute names its effective date on the title page and
  its issue date under the signatures; the file name is consulted only for what the text
  does not say.

## OCR

`parse_fund_documents(..., allow_ocr=False)` — optical recognition is **off by default**,
because recognising every PDF costs far more than it returns. A page that yielded no usable
text is recorded as a `scanned_candidate` instead.

## The AnyDoc layout fallback

AnyDoc is an **optional, opt-in second reading** of PDFs whose layout defeated the primary
parser. It is not installed by default (`pyproject.toml` optional extra `anydoc`) and not
enabled by default (`allow_anydoc_fallback=False`).

Enable it explicitly:

```powershell
uv run fundscraper parse-fund-documents "Fund name" --anydoc-fallback
uv run fundscraper two-pass --anydoc-fallback
```

### When it is attempted

`anydoc_fallback.layout_triggers` returns a non-empty tuple only for a PDF with pages and at
least one of: the primary parse fell back down the parser ladder, a high
`broken_sentence_ratio`, a lot of text with no labelled rows, or a high `multi_column_ratio`.
A trigger only buys a conversion; it does not decide anything.

### When it is refused outright

`blocking_reason` keeps the primary parse whenever the primary already holds the structure
the fallback would flatten: document type `priips_kid` or `financial_statements`, any
detected fee rows, or at least `YEAR_SERIES_ROWS` per-year rows. The guard is written on
what the parse actually holds, not on the type alone.

### When a conversion is accepted

`apply_layout_fallback` compares the two readings and keeps the primary unless the second is
measurably better: word retention must reach `MINIMUM_WORD_RETENTION` and evidence tokens
(years, dates) `MINIMUM_EVIDENCE_RETENTION`, the fund attribution must survive, and pages
must be reconstructable. Outcomes are `not_triggered`, `blocked`, `unavailable`, `failed`,
`rejected`, `accepted`.

### What an accepted value is worth

Enforced once, in `extended_validation.fallback_extraction_metadata`, and used by both
`field_extraction.py` and `extended_extraction.py`:

- every AnyDoc-derived value is `review_required = True`;
- it can never be `HIGH` confidence — high is reduced to medium;
- a value that could not be placed on a page drops to `LOW`.

`conflict_resolution` adds a second guarantee: `parser_provenance` is the **first** rung of
the comparison ladder, so a fallback value never outranks a primary-parser value from a
source of equal or better standing. `validate_fallback_extraction` states the same rule as a
check and is covered by `tests/test_step4_validation.py` and
`tests/test_anydoc_extraction_safety.py`.

This policy exists because the AnyDoc experiment produced values that were plausible and
wrong — a construction progress of 75 % read as a guaranteed minimum return, a per-share
value read as the assets of the fund — created by re-flowing a page so that a label picked
up a number that was never next to it.

## Limitations

- Table detection depends on pymupdf's geometry; a grid drawn with whitespace rather than
  ruling lines may not be detected, and its values are then read from prose.
- Document dates are read from text and file names, not from PDF metadata fields.
- The `document_metadata` table is populated by the parsing stage; a database produced
  before Step 6 may have it empty, in which case consumers derive dates on demand
  (`conflict_resolution.DateReader`).
