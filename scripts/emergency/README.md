# Emergency enrichment tooling

The helpers the emergency enrichment ran on, lifted out of a Claude session scratchpad before it
was cleaned. They produced the 633 values that took delivery coverage from 35.54 % to 52.41 %.

**None of this is production code.** Nothing here is imported by `src/fundscraper/`, nothing here
is covered by the test suite, and nothing here should be wired into a pipeline stage as it stands.
It is preserved for two reasons: the accounting half is the only way to rebuild and re-verify the
current database, and the acquisition half is the written-down form of the source-family knowledge
the refactor has to turn into adapters.

Every helper is annotated below as **TEMPORARY RECOVERY TOOL** or **CANDIDATE FOR FUTURE ADAPTER**.

## Running them

```powershell
uv run python scripts/emergency/<helper>.py [arguments]
```

The acquisition helpers import each other by bare module name (`import em`), which works because
they sit in one directory and are run as scripts. The accounting helpers resolve the repository
root from their own location, so they can be run from anywhere.

Derived accounting is written to `reports/emergency-consolidation/` (gitignored), not next to the
code.

## The accounting helpers — offline, no network

| helper | purpose | input | output |
|---|---|---|---|
| `consolidate.py` | **Rebuilds the current database.** Applies the 18 deltas onto the authoritative baseline in chronological order, reconciling the three applied-value schema generations, and asserts no overwrite, no duplicate application and evidence on every value. | `funds.delivery.offline-recovery1-enriched.json` + `data/output/emergency/*-delta.json` + `data/input/funds.json` | `funds.delivery.current-enriched.json`, `funds.delivery.current-verified.json`, `reports/emergency-consolidation/consolidation-summary.json` |
| `validate.py` | **Independent check of the result.** Deliberately does not import `consolidate`; re-reads everything from disk and asserts the same properties from the outside. Exits non-zero on failure, so it can gate a regeneration. | the two current outputs, the baseline, the deltas, the canonical input | 18 PASS/FAIL lines and both sha256 digests |
| `metrics.py` | Coverage of the current database, recomputed rather than quoted: the delivery figure, the strict fund-level figure that excludes `SOLE_SUBFUND`, the tier split, the per-field breakdown and the completeness distribution. | the two current outputs | `reports/emergency-consolidation/metrics.json` |
| `summary.py` | Per-stage, per-field and per-source-family accounting of the 633 values, including the host classification that established that **no accepted value came from a third-party source**. | `consolidation-summary.json` + the enriched output | `reports/emergency-consolidation/summary.json` |
| `data_quality_report.py` | Renders the 28 unresolved audit flags beside the value that currently stands for each, plus the two review queues (`PROVISIONAL_MEDIUM`, `SCOPED`). | `consolidation-summary.json` + the enriched output | `reports/INTERIM_DATA_QUALITY_ISSUES.md` |
| `state.py` | The effective baseline — the authoritative delivery plus every delta applied so far. Every stage started here, because the question was always "what is still missing after everything so far", never "what does the baseline hold". Also carries the poorest-funds-first work ordering the stages used. | the baseline + the deltas | printed totals; importable as a module |

All six were re-run against the committed data at checkpoint time. `consolidate.py` reproduced both
current outputs byte for byte (`3e07ec5d…` / `a125f0af…`), `validate.py` passed 18 of 18, and
`data_quality_report.py` regenerated `INTERIM_DATA_QUALITY_ISSUES.md` byte-identically.

`consolidate.py` will not silently overwrite: if what it would write differs from what is on disk
it refuses and says so, and `--force` is required to proceed.

## The acquisition helpers — these reach the network

| helper | purpose | source family |
|---|---|---|
| `em.py` | The fetch and read layer everything else sits on: one cache entry per URL, reads walking the whole chain of 17 emergency caches newest first, cp1250-aware HTML text, PyMuPDF page text, link extraction, and a text-layer probe. | all |
| `avanthub.py` | Splits the AVANT hub into one block per fund and per subfond. | AVANT |
| `amistahub.py` | Splits the AMISTA hub into one accordion panel per fund and lists its documents. | AMISTA |
| `orsl2.py` | Session-bound Sbírka-listin access: listing → detail → download inside one client. | public register |
| `register_search.py` | Register name search (prefix, and the CONTAINS/VSECHNY form that finds renamed funds) plus the filing listing. | public register |
| `register_deed.py` | Fetches one filing and reads it, reporting the text layer before printing anything. | public register |
| `grep_document.py` | Probe and grep any document of either kind. | all |
| `probe_page.py` | Probe an HTML page for its dated links — the newsroom and document-panel probe. | all |

**Nothing in this repository runs these automatically.** They fetch only when invoked, and only
what they are pointed at.

## The source-family knowledge worth keeping

This is the part that should survive into adapters. It is written down here because it was learned
by failing at it first.

### AVANT — `avanthub.py`

- `avantfunds.cz/informace-o-fondech/` is **one server-rendered page of about 4 MB**. There is no
  per-fund page to crawl to; a single fetch maps every AVANT fund to its documents.
- One `div.fund-row` per fund **and per subfond**, each carrying
  `data-col-value-for-sorting='<legal name>'` and its own file list. The row is the identity
  evidence; the shared `avantfunds.cz` host is not.
- The richest read inside a row is the annual report's section
  `e) Přehled základních finančních a provozních ukazatelů`, which prints the current and the
  prior period side by side with an explicit `Změna v %`. One table yields
  `assets_under_management` **and** `aum_history`. Section `1. Základní údaje o Fondu` gives
  manager and administrator; the abbreviation table gives IČO and LEI.
- Where AVANT publishes a „neoficiální verze" PDF next to an XHTML/ESEF filing, prefer the PDF.
- 227 values — the largest single source family.

### AMISTA — `amistahub.py`

- **The hub HTML is cp1250, not utf-8.** Decoded as utf-8, every Czech fund name silently fails to
  match and all 103 panels look empty. One decoding flag unlocked 85 values.
- One `div.m-toggle.jq_toggle` per fund, the fund's own name in `span.m-toggle__title`. The panel
  is the identity evidence.
- `download.php?id=` **redirects into a fund-specific folder** (`/files/outuln/`, `/files/ceesic/`,
  …). Following the redirect and reading the folder is a second, independent identity signal.
- The fee scope rule lives in the statute's odst. 12.3 — read it before taking a KID's fees as
  fund-level.

### Sbírka listin / public register — `orsl2.py`, `register_search.py`, `register_deed.py`

- **`vypis-sl-detail` links are session-bound.** Fetched with a fresh client they return
  „Neplatný odkaz". The listing, the detail and the `/ias/content/download` call must share one
  `httpx.Client`. An earlier helper silently returned `None` for every document because of this;
  fixing it made the register the second-largest source family (118 values).
- Search by the **full legal-name prefix** with `typHledani=STARTS_WITH`. A short prefix walks into
  the near-miss trap („TOP ESTATES" → `Top.Estates Sakura s.r.o.`).
- A fund the prefix search cannot find is found by `typHledani=CONTAINS&jenPlatne=VSECHNY`, which
  also matches **deleted** names — the only way to locate a renamed fund. Renames are frequent: 6
  of the 28 unresolved audit flags are identity changes.
- The **úplný výpis** (`rejstrik-firma.vysledky?subjektId=<id>&typ=UPLNY`) carries historic names,
  the board and `v likvidaci`. For a SICAV under § 95 odst. 1 písm. a) ZISIF the sole member of the
  board **is** the obhospodařovatel (§ 9 odst. 1 ZISIF), so the register alone settles `manager`
  even when every filed deed is an image-only scan.
- The register's filing frequently contains the **subfund** report the administrator's panel omits,
  so a shell SICAV still yields a fund-level figure.
- **Probe the text layer before reading.** Newest annual reports filed as ESEF packages serve a
  0-page, ~94-character text layer; image-only scans return 30–300 characters over 30+ pages. Fall
  back one or two years to the nearest readable filing and let `as_of` carry the staleness.

### CODYA, DELTA IS, TILLER / Winstor, PROTON IS, NWD

All five share the AVANT/AMISTA shape — one server-rendered block per fund and per podfond, with a
fund-specific document list — and none has a dedicated helper here; they were read with
`probe_page.py` and `grep_document.py`.

- **CODYA** `codyainvest.cz/nase-fondy/<slug>`, documents behind `file/sdff-get?id=`. Its `Ceník`
  is the fee-scope authority: read it before treating a KID's fees as fund-level. 19 values.
- **DELTA IS** `deltais.cz/fondy/<slug>`. 22 values. A link from a fund site into a DELTA IS panel
  can 404 — that is a signal about the fund's status, not a fetch to retry.
- **TILLER / Winstor** `tillerfunds.cz/cs/portfolio/<slug>` is the **server-rendered replacement**
  for the JS-only `winstor.cz`. 5 values. The same pattern holds elsewhere: `jtis.cz/poradna/…`
  and `redsidefunds.com/cs/fondy/<fund>` are server-rendered, and a JS-only fund site often names
  its administrator's panel in plain text ("na webu administrátora fondu").
- **PROTON IS** `protonis.cz/dokumenty` and **NWD** `nwd.cz/fondy` need **raw-HTML block-title
  matching** — their text layer strips the panel titles, so matching on extracted text finds
  nothing.

### The fund's own site — `probe_page.py`

- 144 values, the second-largest family. Document panels live at `/dokumenty`, `/ke-stazeni`,
  `/povinne-informace`, `/files/`, `?download=` (Joomla), `/wp-content/uploads/` (WordPress).
- On Webflow, Framer, Cloudinary and DatoCMS asset hosts the `<a>` labels are **absent from the
  served HTML**. Grep the raw body for `*.pdf` and identify each asset by its first page.
- Newsroom discovery: `/aktuality`, `/novinky`, `/media`, `/tiskove-zpravy`, WordPress
  `/category/aktuality/`, and for Wix the **sitemap chain** — `/sitemap.xml` →
  `blog-categories-sitemap.xml` / `blog-posts-sitemap.xml`. A Wix newsroom can be absent from both
  the navigation and `pages-sitemap.xml`; the sitemap chain is what closed the last fund of the run.

## Identity and semantics — the rules these tools were operated under

The helpers do not enforce these. A person did, at every value. An adapter must.

- **A manager or administrator domain alone never proves a document belongs to a fund.** The
  fund's own DOM panel or fund-specific document folder does; so does an ISIN, an IČO or the exact
  legal name **inside the document**.
- **Exclusive-host ownership is not identity.** "This host is claimed by only one of our funds,
  therefore its documents are that fund's" produced real misattributions and has been removed from
  the extractor. It must not return.
- A canonical `web` presenting a differently named legal entity is not identity evidence.
- A group newsroom is not a fund newsroom. On a multi-fund domain, filter by fund.
- Never derive a return from two printed NAV-per-share values.
- Never sum share classes or subfunds into a fund-level AUM. If no total is printed, the field
  stays MISSING.
- Prefer `not_found` / `ambiguous` / `review_required` over an unsupported value.

The full list is in `reports/PRE_REFACTOR_HANDOFF.md` §§ 5, 6 and 11.

## Deliberately not preserved

| dropped | why |
|---|---|
| `build17.py` | A one-off builder for the stage-17 delta alone: one hard-coded fund, one hard-coded field, and a `strict_usable_after` figure the reports identify as wrong. The delta it produced is preserved instead. |
| `consolidation-summary.json`, `metrics.json`, `summary.json` (the scratchpad copies) | Derived data, not tooling. Regenerable by the helpers above, and copied into `artifacts/checkpoints/pre-refactor/consolidation/`. |
| the scratchpad's `__pycache__` and session-local paths | Temporary. Every hard-coded absolute path has been replaced with a repository-root resolution. |

## Known limitations

- `em.py` and `orsl2.py` **disable TLS verification**, keep no rate limiting, and send a desktop
  browser user agent. That was a recovery-tool decision to reach misconfigured fund sites under
  time pressure. The production `http_client` does all three properly, and none of it should
  migrate into an adapter.
- The acquisition helpers have no tests and no error handling beyond caching a failure as a
  failure. They are read-and-inspect tools for a person, not services.
- `em.py` writes into `cache/emergency-stage-17/`. The 17 emergency caches hold every document the
  emergency work read and are the cheapest regression corpus for building the adapters — **do not
  delete them.**
