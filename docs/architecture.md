# Architecture

Fundscraper is a single Python package, `src/fundscraper`, driven by a Typer command line
in `cli.py`. There is no service, no queue and no scheduler: a run is a process that walks
a list of funds, and every intermediate result is written to SQLite and the filesystem so
that the next stage can start from it without repeating the previous one.

## Layers

```text
input          models.py, input_loader.py
discovery      official_discovery.py, sitemap_discovery.py, discovery_priority.py,
               domain_adapters/, domain_candidates.py, html_discovery.py
fetching       http_client.py, fetch_service.py, crawl_service.py, site_crawler.py
parsing        document_parser.py, table_extraction.py, document_classification.py,
               document_identity.py, document_dates.py, document_metadata.py,
               anydoc_parser.py, anydoc_fallback.py, document_service.py
extraction     field_extraction.py, extended_extraction.py, field_definitions.py,
               extraction_service.py, extended_persistence.py
decisions      extended_validation.py, conflict_resolution.py, crawl_planning.py
orchestration  pipeline_service.py, two_pass_service.py
delivery       output_models.py, output_service.py, output_audit.py, database.py
```

## The rule of one vocabulary

Two modules are deliberately the only place their kind of decision is made, and everything
else consumes them:

- **`extended_validation.py`** holds `ValidationCode` (the reason-code vocabulary),
  `ValidationSeverity` (`review`, `conflict`, `reject`), `ValidationFinding`, the
  per-field rules and `FIELD_SPECIFICATIONS`. Extraction calls it to refuse a defective
  value; `output_audit.py` calls the same functions to explain a delivered one. There is no
  second validation framework, and adding one would break the guarantee that a defect
  caught before delivery and a defect caught after it carry the same code.
- **`conflict_resolution.py`** holds the deterministic comparison of two candidates that
  claim the same thing, plus the value normalization used to decide whether they disagree
  at all. Both `field_extraction.py` and `extended_extraction.py` route through it.

## State

| Store | Written by | Read by |
| --- | --- | --- |
| `cache/http` | `HttpFetcher` (`http_client.py`) | every fetch, keyed by normalized URL |
| `cache/parsed` | `document_service.py` | extraction, offline re-runs |
| `cache/regen.sqlite3` | `database.py` repositories | extraction, reports, audits |
| `data/output/funds.full.json` | `output_service.write_output` | audit, exports |
| `reports/*.json` | scripts and the CLI | humans |

The database is the index and the filesystem holds the bodies: `sources` and
`parsed_documents` rows point at files under `cache/http` and `cache/parsed`. That split is
why an offline re-extraction can apply new rules to old documents without any network
access.

## Extension points

- A new fund field: add the model to `output_models.py`, the vocabulary to
  `field_definitions.py`, the reader to `field_extraction.py` or `extended_extraction.py`,
  the rule to `extended_validation.py`, and a `FieldSpecification` entry.
- A new document source: prefer a `domain_adapters/` adapter over widening the crawler.
  Adapters run only after official discovery reports itself insufficient.
- A new ranking signal for conflicting sources: add a factor to `ComparisonFactor` and a
  reading to `conflict_resolution._readings`; the ladder order is the priority.

## Limitations

- The pipeline is synchronous per fund; concurrency is across funds and across document
  downloads, not across stages.
- There is no incremental change detection beyond the HTTP cache: a fund is re-crawled in
  full or not at all.
- `run_mvp_final.py` and the `*.ps1` wrappers implement an older three-stage flow
  (official → AVANT → AMISTA). They still run, but the two-pass crawl in
  `two_pass_service.py` supersedes them.
