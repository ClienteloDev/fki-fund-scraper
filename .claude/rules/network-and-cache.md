# Network and cache rules

Prefer cached/offline testing.

Never run a full online crawl without explicit instruction.

Preserve:

- cache/regen.sqlite3
- cache/http
- cache/parsed

Never register only a subset into the production database.

Never use a stale sample dataset as canonical input.

Avoid re-downloading known URLs unless explicitly forced.

Respect per-domain concurrency and rate limiting.
