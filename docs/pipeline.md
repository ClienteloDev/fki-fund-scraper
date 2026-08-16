# Pipeline

One fund passes through six stages. `pipeline_service.run_fund_pipeline` runs all of them;
each also has its own CLI command so a single stage can be repeated in isolation.

```text
fund input
  -> official discovery          official_discovery.discover_official_sources
  -> fallback discovery          pipeline_service._discover_adapter_sources
  -> authoritative-copy filter   crawl_planning.select_authoritative_documents
  -> crawl and download          crawl_service.crawl_fund_site
  -> parsing                     document_service.parse_fund_documents
  -> extraction                  extraction_service.extract_fund_data
       -> validation             extended_validation
       -> conflict resolution    conflict_resolution
  -> output + audit              output_service, output_audit
```

## 1. Official discovery

`discover_official_sources` explores the fund's own website, or the fund's profile on its
manager's site, and collects the official documents it links to. It is always first: an
external source may only add what the fund itself does not publish.

The result reports `is_exhausted` (the walk ran out of pages rather than budget) and
`is_sufficient` (every required document group was found). Only the second may skip a
fallback. See [discovery.md](discovery.md).

## 2. Fallback discovery

When `is_sufficient` is false, `_discover_adapter_sources` runs the adapters in
`domain_adapters/` — an explicit adapter for the fund's domain when one is registered, plus
whichever of the AVANT, AMISTA and porovnejfondy fallbacks the caller enabled. Their
documents are merged with the official ones by canonical URL, higher score winning.

When `is_sufficient` is true the adapter result is replaced by a placeholder named
`official_only` and no adapter runs.

## 3. Authoritative-copy filter

`select_authoritative_documents` reduces the merged list to one copy per document and
revision before anything is downloaded: the newest revision of a statute, key information
document, memorandum, prospectus or price list, but every distinct year of a reporting
document. The count it dropped is reported as `superseded_documents`.

This exists because most disagreements between sources turn out to be one document
published twice or a revision sitting next to its predecessor.

## 4. Crawl and download

`crawl_fund_site` seeds a breadth-first walk from the fund website plus the navigation URLs
discovery returned, downloads the document links, and records each source in the `sources`
table. Limits: `max_pages`, `max_depth`, `max_documents`, `document_concurrency`.

## 5. Parsing

`parse_fund_documents` converts every downloaded source into normalized text plus page and
table structure under `cache/parsed`, and records a `parsed_documents` row. OCR
(`allow_ocr`) and the AnyDoc layout fallback (`allow_anydoc_fallback`) are both off by
default. See [parsing.md](parsing.md).

## 6. Extraction, validation, conflict resolution

`extract_fund_data` loads the parsed documents, runs `extract_fund_fields` and
`extract_extended_fields`, writes the fund's record into the output file and persists the
extended rows into SQLite. Validation and conflict resolution are not separate stages: they
run inside extraction, which is why a refused value never reaches the output as if it had
been found. See [extraction.md](extraction.md), [validation.md](validation.md) and
[conflict-resolution.md](conflict-resolution.md).

## Batch and two-pass orchestration

- `pipeline_service.run_fund_batch` runs many funds concurrently with a shared output lock.
- `two_pass_service.run_two_pass` runs the batch twice at different budgets, selecting the
  second round from the delivered output, the audit report and the conflict report. See
  [crawler.md](crawler.md).

## Status semantics

`extract_fund_data` sets `ProcessingStatus.COMPLETED` when all five *delivered* fields were
found and `PARTIAL` otherwise; the extended fields do not affect it.
`run_fund_pipeline` maps the same condition onto `FundStatus.COMPLETED` / `PARTIAL`, and any
exception to `FAILED` with the message appended to the fund's processing warnings.

## Repeating a single stage

```powershell
uv run fundscraper fetch-start-page "Fund name"
uv run fundscraper discover-start-page "Fund name"
uv run fundscraper crawl-fund "Fund name"
uv run fundscraper discover-official "Fund name"
uv run fundscraper parse-fund-documents "Fund name"
uv run fundscraper extract-fund "Fund name"
uv run fundscraper run-fund "Fund name"
```

Each takes `--input`, `--database` and the relevant cache directory; run any of them with
`--help` for the exact options. `extract-fund` and `parse-fund-documents` need no network.
