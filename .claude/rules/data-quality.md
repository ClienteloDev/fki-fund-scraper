# Data quality rules

Correctness is more important than coverage.

A missing value is preferable to a plausible but unsupported value.

Every accepted value must have sufficient provenance.

Do not infer fund ownership from a manager domain alone.

Preserve competing candidates when sources conflict.

Suspicious values remain available with:

- review_required
- reason
- source
- evidence

Do not silently coerce:

- percentages into amounts
- per-share values into AUM
- historical returns into target returns
- manager-level metrics into fund-level metrics
- one share class into another
