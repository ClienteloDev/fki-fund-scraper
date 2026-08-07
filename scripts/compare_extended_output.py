"""
Compare two enriched output files field by field.

The script is read-only and performs no network access. It reports how
the status of every field changed between a baseline run and a new one,
which is what proves that a change in the extraction rules improved the
result instead of only moving it.

Fields the baseline did not know are reported on their own, because a
field that did not exist cannot be said to have regressed.

Usage:

    uv run python scripts/compare_extended_output.py \\
        --baseline data/output/funds.reextracted.json \\
        --current data/output/funds.step3-targeted.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DELIVERED_FIELDS = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
)


EXTENDED_FIELDS = (
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
)


def load(path: Path) -> dict[str, dict[str, Any]]:
    """Read one output file, keyed by fund name."""

    payload = json.loads(path.read_text(encoding="utf-8-sig"))

    if not isinstance(payload, list):
        raise ValueError(f"The root value of {path} must be an array of funds")

    records: dict[str, dict[str, Any]] = {}

    for item in payload:
        if isinstance(item, dict):
            records[str(item.get("name") or "")] = item

    return records


def status_of(
    record: dict[str, Any],
    field: str,
) -> str:
    payload = record.get(field)

    if not isinstance(payload, dict):
        return "absent"

    return str(payload.get("status") or "absent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_extended_output",
        description=("Report how the status of every field changed between two output files."),
    )

    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="Output file produced before the change.",
    )
    parser.add_argument(
        "--current",
        type=Path,
        required=True,
        help="Output file produced after the change.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional path for a JSON copy of the comparison.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        baseline = load(args.baseline.resolve())

        current = load(args.current.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"Comparison failed: {exc}",
            file=sys.stderr,
        )

        return 1

    shared = [name for name in current if name in baseline]

    changes: list[dict[str, str]] = []

    delivered_counts: dict[str, Counter[str]] = {field: Counter() for field in DELIVERED_FIELDS}

    extended_counts: dict[str, Counter[str]] = {field: Counter() for field in EXTENDED_FIELDS}

    for name in shared:
        for field in DELIVERED_FIELDS:
            before = status_of(
                baseline[name],
                field,
            )

            after = status_of(
                current[name],
                field,
            )

            delivered_counts[field][f"{before} -> {after}"] += 1

            if before != after:
                changes.append(
                    {
                        "fund": name,
                        "field": field,
                        "before": before,
                        "after": after,
                    }
                )

        for field in EXTENDED_FIELDS:
            extended_counts[field][status_of(current[name], field)] += 1

    print(f"Baseline : {args.baseline}")
    print(f"Current  : {args.current}")
    print(f"Funds compared: {len(shared)}")
    print()

    print("DELIVERED FIELDS (status before -> after)")
    print("-" * 70)

    for field in DELIVERED_FIELDS:
        moves = delivered_counts[field]

        unchanged = sum(
            count for move, count in moves.items() if move.split(" -> ")[0] == move.split(" -> ")[1]
        )

        print(f"  {field:<26}unchanged={unchanged:<5}changed={sum(moves.values()) - unchanged}")

        for move, count in sorted(moves.items()):
            if move.split(" -> ")[0] != move.split(" -> ")[1]:
                print(f"      {move:<40}{count}")

    print()
    print("EXTENDED FIELDS (new in schema version 3)")
    print("-" * 70)

    for field in EXTENDED_FIELDS:
        summary = "  ".join(
            f"{status}={count}" for status, count in sorted(extended_counts[field].items())
        )

        print(f"  {field:<26}{summary}")

    if args.report is not None:
        report_path = args.report.resolve()

        report_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        report_path.write_text(
            json.dumps(
                {
                    "baseline": str(args.baseline),
                    "current": str(args.current),
                    "funds_compared": len(shared),
                    "delivered_field_changes": changes,
                    "delivered_field_moves": {
                        field: dict(sorted(moves.items()))
                        for field, moves in delivered_counts.items()
                    },
                    "extended_field_statuses": {
                        field: dict(sorted(counts.items()))
                        for field, counts in extended_counts.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print()
        print(f"Full comparison: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
