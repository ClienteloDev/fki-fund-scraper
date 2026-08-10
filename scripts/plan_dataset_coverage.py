"""
Report which funds of the canonical dataset still need work.

The dataset grows between runs, so the number of funds is always read
from the file rather than assumed. Every fund is placed in exactly one
group so that a later run can process only what is missing instead of
crawling everything again.

The script only reads. It performs no network access and writes nothing
but its own work lists.

Usage:

    uv run python scripts/plan_dataset_coverage.py \\
        --input data/input/funds.json \\
        --database cache/regen.sqlite3 \\
        --output-directory data/input
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from fundscraper.input_loader import InputFileError, load_funds
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id


@dataclass(frozen=True, slots=True)
class Coverage:
    """How the canonical dataset divides into work that remains."""

    total: int
    covered: tuple[FundInput, ...]
    new_with_website: tuple[FundInput, ...]
    without_website: tuple[FundInput, ...]

    @property
    def outstanding(self) -> int:
        return len(self.new_with_website) + len(self.without_website)


def parsed_fund_ids(
    database_path: Path,
) -> set[str]:
    """Return the funds that already have at least one parsed document."""

    if not database_path.exists():
        return set()

    try:
        with sqlite3.connect(database_path) as connection:
            rows = connection.execute(
                "SELECT DISTINCT fund_id FROM parsed_documents",
            ).fetchall()
    except sqlite3.Error:
        return set()

    return {str(row[0]) for row in rows}


def plan_coverage(
    *,
    funds: list[FundInput],
    covered_ids: set[str],
) -> Coverage:
    """Split the dataset into what is done and what is not."""

    covered: list[FundInput] = []

    new_with_website: list[FundInput] = []

    without_website: list[FundInput] = []

    for fund in funds:
        if stable_fund_id(fund) in covered_ids:
            covered.append(fund)

            continue

        if fund.has_website:
            new_with_website.append(fund)
        else:
            # No address to crawl. Only a manager profile or a fallback
            # adapter can reach such a fund.
            without_website.append(fund)

    return Coverage(
        total=len(funds),
        covered=tuple(covered),
        new_with_website=tuple(new_with_website),
        without_website=tuple(without_website),
    )


def write_list(
    *,
    path: Path,
    funds: tuple[FundInput, ...],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            [
                {
                    "name": fund.name,
                    "web": fund.web,
                }
                for fund in funds
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan_dataset_coverage",
        description=("Report which funds of the canonical dataset still need crawling."),
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/funds.json"),
        help="The canonical fund dataset.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("cache/regen.sqlite3"),
        help="Processing database of the previous run.",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("data/input"),
        help="Where the work lists are written.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        funds = load_funds(args.input.resolve())
    except InputFileError as exc:
        print(
            f"Canonical dataset could not be loaded: {exc}",
            file=sys.stderr,
        )

        return 1

    coverage = plan_coverage(
        funds=funds,
        covered_ids=parsed_fund_ids(args.database.resolve()),
    )

    directory = args.output_directory.resolve()

    write_list(
        path=directory / "funds.to-crawl.json",
        funds=coverage.new_with_website,
    )

    write_list(
        path=directory / "funds.without-website.json",
        funds=coverage.without_website,
    )

    print(f"Canonical dataset      : {args.input} ({coverage.total} funds)")
    print(f"Already covered        : {len(coverage.covered)}")
    print(f"New, with a website    : {len(coverage.new_with_website)}")
    print(f"Without a website      : {len(coverage.without_website)}")
    print(f"Outstanding            : {coverage.outstanding}")
    print()
    print(f"Work list  : {directory / 'funds.to-crawl.json'}")
    print(f"No website : {directory / 'funds.without-website.json'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
