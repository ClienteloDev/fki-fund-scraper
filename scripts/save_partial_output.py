"""
Preserve the finished part of an interrupted pipeline run.

The script is read-only towards the run itself. It copies the records
that were already processed into a clearly marked partial output, writes
a manifest describing what the file contains, and generates an input file
holding only the funds that still have to be processed.

Usage:

    uv run python scripts/save_partial_output.py \\
        --output data/output/funds.regenerated.json \\
        --input data/input/funds.audit-rerun.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROCESSED_STATUSES = frozenset(
    {
        "completed",
        "partial",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="save_partial_output",
        description=(
            "Save the processed records of an interrupted run as a partial "
            "output and prepare an input file with the remaining funds."
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/output/funds.regenerated.json"),
        help="Enriched output written by the interrupted run.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/funds.audit-rerun.json"),
        help="Input file the interrupted run was started with.",
    )
    parser.add_argument(
        "--partial-output",
        type=Path,
        default=None,
        help="Where to write the partial output (defaults next to --output).",
    )
    parser.add_argument(
        "--resume-input",
        type=Path,
        default=None,
        help="Where to write the input holding the remaining funds.",
    )

    return parser


def write_json(
    path: Path,
    payload: Any,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(path)


def main() -> int:
    args = build_parser().parse_args()

    output_path = args.output.resolve()

    if not output_path.exists():
        print(
            f"Output of the interrupted run does not exist: {output_path}",
            file=sys.stderr,
        )

        return 1

    records = json.loads(output_path.read_text(encoding="utf-8-sig"))

    if not isinstance(records, list):
        print(
            "The output must be an array of funds.",
            file=sys.stderr,
        )

        return 1

    processed: list[dict[str, Any]] = []

    remaining: list[dict[str, Any]] = []

    failed: list[str] = []

    for record in records:
        status = str((record.get("processing") or {}).get("status") or "pending")

        if status == "failed":
            failed.append(str(record.get("name")))

        if status in PROCESSED_STATUSES:
            processed.append(record)
        else:
            remaining.append(record)

    partial_path = args.partial_output or output_path.with_name(f"{output_path.stem}.partial.json")

    resume_path = args.resume_input or args.input.resolve().with_name(
        f"{args.input.stem}.remaining.json"
    )

    manifest_path = partial_path.with_name(f"{partial_path.stem}.manifest.json")

    write_json(
        partial_path,
        processed,
    )

    write_json(
        resume_path,
        [
            {
                "name": record.get("name"),
                "web": record.get("web"),
            }
            for record in remaining
        ],
    )

    completed = sum(
        1
        for record in processed
        if str((record.get("processing") or {}).get("status")) == "completed"
    )

    field_names = (
        "investment_horizon",
        "minimum_investment",
        "target_return",
        "fees",
        "assets_under_management",
    )

    found_values = sum(
        1
        for record in processed
        for field in field_names
        if str((record.get(field) or {}).get("status")) == "found"
    )

    manifest = {
        "partial": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_output": str(output_path),
        "source_input": str(args.input.resolve()),
        "funds_in_run": len(records),
        "funds_processed": len(processed),
        "funds_completed": completed,
        "funds_partial": len(processed) - completed,
        "funds_failed": len(failed),
        "funds_remaining": len(remaining),
        "found_values": found_values,
        "failed_funds": failed,
        "partial_output": str(partial_path),
        "resume_input": str(resume_path),
        "note": (
            "This output holds only the funds the interrupted run finished. "
            "It is not a complete result and must not be delivered as one."
        ),
    }

    write_json(
        manifest_path,
        manifest,
    )

    print(f"Funds in the run     : {len(records)}")
    print(f"Processed and saved  : {len(processed)} ({completed} complete)")
    print(f"Failed               : {len(failed)}")
    print(f"Remaining            : {len(remaining)}")
    print(f"Found values saved   : {found_values}")
    print()
    print(f"Partial output       : {partial_path}")
    print(f"Manifest             : {manifest_path}")
    print(f"Resume input         : {resume_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
