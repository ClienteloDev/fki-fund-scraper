# Fundscraper

## Mission

Build a reliable, auditable scraper for Czech qualified-investor funds.

Prefer correctness and traceability over maximizing the number of extracted values.

A missing value is better than an unsupported or incorrectly attributed value.

## Development approach

Use test-driven development for bug fixes and non-trivial behavior changes:

1. reproduce the issue with a focused test
2. verify the test fails for the expected reason
3. implement the smallest safe change
4. run targeted tests
5. run the full verification suite

For existing production behavior, prefer regression tests based on real cached documents and real failure cases.

## Working style

Before changing code:

1. inspect the existing implementation
2. identify the smallest appropriate extension point
3. reuse existing abstractions
4. do not create parallel frameworks
5. avoid unrelated refactoring
6. explain any important remaining limitation

Prefer simple, explicit implementations over clever abstractions.

## Canonical project data

Canonical input:

`data/input/funds.json`

Never hard-code the number of funds.

Production database/cache:

`cache/regen.sqlite3`

HTTP cache:

`cache/http`

Parsed cache:

`cache/parsed`

Current full output:

`data/output/funds.full.json`

Never use old sample or audit datasets as the canonical full-run input.

## Pipeline

High-level flow:

fund input
→ official discovery
→ fallback discovery
→ HTTP/document cache
→ document parsing
→ extraction
→ validation
→ conflict resolution
→ audit/output

The crawler uses a two-pass strategy:

- fast pass for all funds
- targeted deep pass only for unresolved/problem fields

See project documentation for implementation details.

## Source policy

Prefer:

1. official fund source
2. official fund-specific manager/administrator source
3. official manager source
4. configured fallback source

A manager domain alone does not prove that a document belongs to a fund.

Always preserve fund/subfund/share-class attribution.

## Extraction policy

Every accepted value should preserve, where applicable:

- source
- evidence / quote
- page
- scope
- confidence
- review_required

Do not silently discard suspicious or conflicting candidates.

Prefer `not_found`, `ambiguous` or `review_required` over an unsupported value.

## Validation

Validation is centralized in the existing validation architecture.

Do not create a second validation system.

Suspicious values are retained and marked for review.

Clearly wrong semantic interpretations may be rejected.

Conflicts should preserve competing candidates.

## Parsing

The structured Step 6 parser is the primary parser.

AnyDoc is optional and disabled by default.

AnyDoc-derived values:

- remain review_required
- must not be HIGH confidence
- must not outrank strong primary-parser official evidence

## Database and cache safety

SQLite migrations must be additive unless explicitly approved otherwise.

Never register only a subset of funds into the production database.

Always synchronize against the full canonical dataset.

Do not delete or recreate the production cache as part of routine development.

Reuse HTTP and parsed caches whenever possible.

## Network policy

Do not run a full online crawl unless explicitly requested.

During development prefer:

1. unit tests
2. targeted cached sample
3. full offline extraction
4. targeted online test
5. full online run only when requested

Do not use `--force` unless there is a specific reason to bypass cache.

## Testing

Targeted tests first.

Before declaring implementation complete run:

`uv run pytest -q`

`uv run mypy`

`uv run ruff check src tests scripts`

When changing formatting-sensitive files also run:

`uv run ruff format --check src tests`

## Definition of done

A task is complete only when:

- implementation matches the requested behavior
- regression tests exist where appropriate
- targeted tests pass
- full pytest passes
- mypy passes
- Ruff passes
- no unintended network requests occurred
- existing output/schema compatibility is preserved unless intentionally changed
- remaining limitations are reported

## Git

Do not commit unless explicitly requested.

Do not modify unrelated files merely to satisfy formatting or linting.

## Documentation

When architecture, commands, schema or pipeline behavior changes, update the relevant documentation.

README is the human onboarding entry point.

Detailed implementation belongs in `docs/`, not in CLAUDE.md.

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature-slug>/` in this repo, committed alongside the code. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, used verbatim: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` at the repo root plus `docs/adr/`, both created lazily. See `docs/agents/domain.md`.
