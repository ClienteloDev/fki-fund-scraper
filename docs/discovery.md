# Discovery

Discovery decides which pages and documents of a fund are worth fetching. It never
downloads documents itself: `discover_official_sources` returns navigation URLs and
`DiscoveredLink` seeds, and the crawler downloads them.

Source files: `official_discovery.py`, `sitemap_discovery.py`, `discovery_priority.py`,
`html_discovery.py`, `domain_adapters/`.

## Official first

`official_discovery.discover_official_sources(database_path, fund, fetcher, ...)` walks the
fund's own domain, or the fund's section of its manager's site, using a priority queue
(`heapq`) rather than breadth-first order, so the pages most likely to hold documents are
fetched first within the budget.

Seeds, in descending priority: `robots.txt` sitemap references (200), the conventional
sitemap paths from `candidate_sitemap_urls` (190), and the fund website itself (1 000).

`robots.txt` is honoured by default (`respect_robots=True`): `robots_disallowed_paths` and
`is_allowed` in `sitemap_discovery.py`.

## Sitemaps

`parse_sitemap` reads both sitemap indexes and URL sets, with a regex fallback for
malformed XML. Nested sitemaps are followed to `MAXIMUM_SITEMAP_DEPTH`, and at most
`MAXIMUM_SITEMAP_DOCUMENTS` sitemaps are read per fund. `filter_sitemap_entries` scores
entries before any of them is fetched, so a large sitemap does not consume the page budget.

## Prioritization

`discovery_priority.score_link(signals, wanted_document_types=...)` adds independent
evidence rather than picking a single winner:

| Signal | Source |
| --- | --- |
| Document keyword | `DOCUMENT_KEYWORDS` — `priips`, `statut`, `vyrocni zprava`, `factsheet`, … |
| Section keyword | `SECTION_KEYWORDS` — `povinne informace`, `ke stazeni`, `pro investory`, … |
| Fund keyword | `FUND_KEYWORDS` — `podfond`, `sicav`, `share class`, … |
| Fund name tokens | `distinctive_tokens` of the fund name found in the link text |
| Document type | half of `DOCUMENT_TYPE_RANK` |
| Wanted document type | `WANTED_DOCUMENT_TYPE_BONUS` (60), used by the deep pass |
| Year | up to +20, newest preferred, older still positive |
| Unrelated page | −80 for `UNRELATED_KEYWORDS` (careers, GDPR, cookies, login, …) |

`searchable_text` turns URL separators into spaces, so `vyrocni-zprava-2024.pdf` matches the
same keywords as the anchor text. Thresholds: `NAVIGATION_THRESHOLD` (30) to fetch a page,
`DOCUMENT_THRESHOLD` (40) to accept a document.

## Attribution

A domain never proves ownership. `_scope_of` classifies each document as
`exact_fund`, `own_official_domain` or `fund_section_of_manager_site`, and
`_named_document_type` plus `OTHER_FUND_MARKERS` reject a document that names a *different*
fund. A manager site hosting many funds is attributed by section or profile, never
wholesale.

Rejection reasons recorded per link: `off_domain`, `low_priority`, `unrelated_page`,
`disallowed_by_robots`, `belongs_to_another_fund`, `duplicate_document`,
`crawl_budget_exhausted`, `fetch_failed`.

## Sufficiency

`REQUIRED_DOCUMENT_GROUPS` defines what a fund is expected to publish:

| Group | Accepted types |
| --- | --- |
| `key_information` | `priips_kid` |
| `governing_document` | `statute`, `subfund_statute`, `memorandum` |
| `reporting_document` | `annual_report`, `financial_statements`, `half_year_report`, `factsheet` |

`missing_document_groups` lists the groups not covered; `is_sufficient` is true when none is
missing. `is_exhausted` is a different fact — it says the walk ended because the frontier
emptied rather than because the budget ran out — and only `is_sufficient` may skip a
fallback.

When a caller passes `wanted_document_types`, **any one** of them satisfies the request; the
missing entry is reported as a single `wanted:a|b|c` string rather than one line per type.
`stop_when_sufficient` ends the walk as soon as the requirement is met, which is what the
fast pass uses.

## Observability

Every inspected link becomes a `DiscoveryEntry` and, when a database path is given, a row in
the `discovery_log` table: URL, `discovered_from`, discovery method
(`seed`, `navigation`, `sitemap`, `document_link`, `manager_profile`, `fallback_adapter`),
priority score, document type, scope decision, accepted flag, rejection reason and run id.

```powershell
uv run fundscraper discover-official "Fund name" --show-rejected
```

## Fallback adapters

`domain_adapters/` holds AVANT, AMISTA, BHS and porovnejfondy adapters plus the registry.
They run only after official discovery reports itself insufficient, and only when the caller
enables them. `inspect-adapter` runs one without extraction.

## Limitations

- Scope classification is textual. A manager page that names two funds in one section can
  still attribute a document to the wrong one; the audit's `source_shared_across_funds`
  finding is the backstop.
- Sitemap filtering uses the same keyword scoring as link scoring, so a fund whose documents
  carry no recognisable wording is found only through navigation.
- JavaScript-rendered navigation is not executed; `PLAYWRIGHT_ENABLED` appears in
  `.env.example` but no code reads it.
