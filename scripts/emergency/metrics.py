"""
Coverage metrics of the current database, recomputed from the file.

Nothing here is copied from a report. The two current outputs are digested, the
fund count is asserted, every tier is counted and the delivery projection is
checked against the enriched file before a single number is printed.

The two coverage figures both matter:

- **delivery** counts every usable slot, including the 217 whose ``scope_class``
  is ``SOLE_SUBFUND`` - the fund's only subfond, so the scope equals the
  delivery record;
- **strict fund-level** excludes them, because that equality is an assumption
  and a consumer who does not share it needs the number without a re-run.

``SCOPED`` values - one of several subfonds or classes - are in neither figure;
they live in ``scoped_values`` and are reported separately.

Writes ``reports/emergency-consolidation/metrics.json``.

CANDIDATE FOR FUTURE ADAPTER - not an adapter, but the coverage definition here
is the one the refactor has to keep reproducing.

Usage::

    uv run python scripts/emergency/metrics.py
"""

from __future__ import annotations

import collections
import hashlib
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

ENRICHED = REPOSITORY_ROOT / "data/output/funds.delivery.current-enriched.json"

VERIFIED = REPOSITORY_ROOT / "data/output/funds.delivery.current-verified.json"

WORK = REPOSITORY_ROOT / "reports/emergency-consolidation"

FIELDS = [
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
]


def status_of(slot):
    return (slot or {}).get("status", "MISSING")


def main():
    records = json.loads(ENRICHED.read_text(encoding="utf-8"))

    projection = json.loads(VERIFIED.read_text(encoding="utf-8"))

    assert len(records) == len(projection), "enriched and verified disagree on the fund count"

    funds = len(records)

    slots = funds * len(FIELDS)

    tiers: collections.Counter[str] = collections.Counter()

    per_field = {field: collections.Counter() for field in FIELDS}

    per_field_strict: collections.Counter[str] = collections.Counter()

    distribution: collections.Counter[int] = collections.Counter()

    complete, strict_usable, scoped_total = [], 0, 0

    for record in records:
        usable_here = 0

        for field in FIELDS:
            slot = record.get(field)

            tier = status_of(slot)

            tiers[tier] += 1

            per_field[field][tier] += 1

            if tier != "MISSING":
                usable_here += 1

                if slot.get("scope_class") != "SOLE_SUBFUND":
                    strict_usable += 1

                    per_field_strict[field] += 1

        scoped_total += len(record.get("scoped_values", []))

        distribution[usable_here] += 1

        if usable_here == len(FIELDS):
            complete.append(record["name"])

    usable = slots - tiers["MISSING"]

    found = sum(
        1 for record in projection for field in FIELDS if record[field]["status"] == "found"
    )

    assert found == usable, (found, usable)

    assert sum(distribution.values()) == funds

    out = {
        "files": {
            "enriched": {
                "path": "data/output/funds.delivery.current-enriched.json",
                "sha256": hashlib.sha256(ENRICHED.read_bytes()).hexdigest(),
            },
            "verified": {
                "path": "data/output/funds.delivery.current-verified.json",
                "sha256": hashlib.sha256(VERIFIED.read_bytes()).hexdigest(),
            },
        },
        "funds": funds,
        "theoretical_slots": slots,
        "delivery_usable": usable,
        "delivery_coverage_pct": round(usable / slots * 100, 2),
        "strict_fund_level_usable": strict_usable,
        "strict_fund_level_coverage_pct": round(strict_usable / slots * 100, 2),
        "VERIFIED": tiers["VERIFIED"],
        "PROVISIONAL_HIGH": tiers["PROVISIONAL_HIGH"],
        "PROVISIONAL_MEDIUM": tiers["PROVISIONAL_MEDIUM"],
        "MISSING": tiers["MISSING"],
        "scoped_but_not_counted": scoped_total,
        "per_field": {
            field: {
                "usable": funds - per_field[field]["MISSING"],
                "missing": per_field[field]["MISSING"],
                "verified": per_field[field]["VERIFIED"],
                "provisional": (
                    per_field[field]["PROVISIONAL_HIGH"] + per_field[field]["PROVISIONAL_MEDIUM"]
                ),
                "provisional_high": per_field[field]["PROVISIONAL_HIGH"],
                "provisional_medium": per_field[field]["PROVISIONAL_MEDIUM"],
                "strict_usable": per_field_strict[field],
            }
            for field in FIELDS
        },
        "distribution": {
            f"{count}/{len(FIELDS)}": distribution.get(count, 0) for count in range(len(FIELDS) + 1)
        },
        "complete_funds": sorted(complete),
    }

    WORK.mkdir(parents=True, exist_ok=True)

    (WORK / "metrics.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    main()
