"""
Describe the authoritative 341-fund delivery generation exactly as it is on disk.

Everything here is read-only and verified rather than assumed: the file is
digested, the fund count is asserted against the canonical input, and every
count is recomputed from the file rather than copied from an earlier report.

Usage::

    uv run python scripts/offline_baseline.py [--enriched PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]

sys.path.insert(0, str(SCRIPT_DIRECTORY))

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_selection import DELIVERY_FIELDS  # noqa: E402

from fundscraper.input_loader import load_funds  # noqa: E402

USABLE = ("VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM")


def digest(path: Path) -> str:
    hasher = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)

    return hasher.hexdigest()


def bucket(count: int) -> str:
    if count == 0:
        return "0"

    if count == 1:
        return "1"

    if count == 2:
        return "2"

    if count <= 5:
        return "3-5"

    return "6+"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(prog="offline_baseline")

    parser.add_argument(
        "--enriched",
        type=Path,
        default=REPOSITORY_ROOT / "data/output/funds.delivery.autonomous4-enriched.json",
    )

    parser.add_argument("--report-stem", default="full-offline-recovery-before")

    arguments = parser.parse_args()

    path = arguments.enriched.resolve()

    records = json.loads(path.read_text(encoding="utf-8"))

    canonical = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    canonical_names = [fund.name for fund in canonical]

    by_status: Counter[str] = Counter()

    per_field: dict[str, Counter[str]] = {field: Counter() for field in DELIVERY_FIELDS}

    per_fund_usable: dict[str, int] = {}

    for record in records:
        usable = 0

        for field in DELIVERY_FIELDS:
            payload = record.get(field)

            status = (payload or {}).get("status") if isinstance(payload, dict) else None

            status = status or "MISSING"

            by_status[status] += 1

            per_field[field][status] += 1

            if status in USABLE:
                usable += 1

        per_fund_usable[str(record.get("name") or "")] = usable

    slots = len(records) * len(DELIVERY_FIELDS)

    usable_total = sum(by_status[status] for status in USABLE)

    distribution = Counter(bucket(count) for count in per_fund_usable.values())

    payload = {
        "input_filename": str(path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "sha256": digest(path),
        "records": len(records),
        "canonical_funds": len(canonical),
        "fund_count_matches_canonical_input": len(records) == len(canonical),
        "fund_order_matches_canonical_input": [r.get("name") for r in records] == canonical_names,
        "fields_per_fund": len(DELIVERY_FIELDS),
        "theoretical_slots": slots,
        "usable_total": usable_total,
        "verified": by_status.get("VERIFIED", 0),
        "provisional_high": by_status.get("PROVISIONAL_HIGH", 0),
        "provisional_medium": by_status.get("PROVISIONAL_MEDIUM", 0),
        "missing": by_status.get("MISSING", 0),
        "coverage": round(usable_total / slots, 6),
        "status_counts": dict(by_status.most_common()),
        "per_field": {
            field: {
                "verified": counts.get("VERIFIED", 0),
                "provisional_high": counts.get("PROVISIONAL_HIGH", 0),
                "provisional_medium": counts.get("PROVISIONAL_MEDIUM", 0),
                "usable": sum(counts.get(status, 0) for status in USABLE),
                "missing": counts.get("MISSING", 0),
            }
            for field, counts in per_field.items()
        },
        "funds_by_usable_fields": {
            key: distribution.get(key, 0) for key in ("0", "1", "2", "3-5", "6+")
        },
        "per_fund_usable": per_fund_usable,
    }

    (REPOSITORY_ROOT / f"reports/{arguments.report_stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Full dataset — baseline before the offline recovery run",
        "",
        f"File: `{payload['input_filename']}`",
        "",
        f"SHA256 `{payload['sha256']}`",
        "",
        f"- records: **{payload['records']}** "
        f"(canonical input has {payload['canonical_funds']}; "
        f"match: {payload['fund_count_matches_canonical_input']}, "
        f"same order: {payload['fund_order_matches_canonical_input']})",
        f"- slots: **{payload['theoretical_slots']}**",
        f"- usable: **{payload['usable_total']}** · coverage **{payload['coverage']:.2%}**",
        f"- VERIFIED {payload['verified']} · PROVISIONAL_HIGH {payload['provisional_high']} "
        f"· PROVISIONAL_MEDIUM {payload['provisional_medium']} · MISSING {payload['missing']}",
        "",
        "## Per field",
        "",
        "| field | usable | VERIFIED | PROV_HIGH | PROV_MED | missing |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    for field, counts in payload["per_field"].items():  # type: ignore[union-attr]
        lines.append(
            f"| {field} | {counts['usable']} | {counts['verified']} | "
            f"{counts['provisional_high']} | {counts['provisional_medium']} | {counts['missing']} |"
        )

    lines.extend(
        [
            "",
            "## Funds by number of usable fields",
            "",
            "| usable fields | funds |",
            "|---|---:|",
        ]
    )

    for key, count in payload["funds_by_usable_fields"].items():  # type: ignore[union-attr]
        lines.append(f"| {key} | {count} |")

    (REPOSITORY_ROOT / f"reports/{arguments.report_stem}.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print("\n".join(lines))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
