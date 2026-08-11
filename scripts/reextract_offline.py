"""
Re-run field extraction over already parsed documents.

The script performs no network access and downloads nothing. It reads the
documents a previous run stored in the processing database and applies the
current extraction, scope and normalization rules to them, so a change in
those rules can be measured without crawling again.

The processing database is only read, so the original run stays intact.

Usage:

    uv run python scripts/reextract_offline.py \\
        --input data/input/funds.json \\
        --database cache/regen.sqlite3 \\
        --output data/output/funds.reextracted.json
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from fundscraper.database import DatabaseError, list_parsed_documents
from fundscraper.document_parser import DocumentParseError, load_parsed_document
from fundscraper.extended_extraction import extract_extended_fields
from fundscraper.field_extraction import ExtractionDocument, extract_fund_fields
from fundscraper.input_loader import InputFileError, load_funds
from fundscraper.models import FundInput
from fundscraper.output_models import FieldStatus, ProcessingMetadata, ProcessingStatus
from fundscraper.output_service import (
    create_pending_fund,
    stable_fund_id,
    write_output,
)

FIELD_NAMES = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
)


# The fields added in schema version 3. They are counted separately so
# that the delivered coverage of a run stays comparable with earlier
# runs, which did not know these fields at all.
EXTENDED_FIELD_NAMES = (
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
)


def _load_documents(
    *,
    database_path: Path,
    fund: FundInput,
) -> list[ExtractionDocument]:
    """Load the documents a previous run parsed for one fund."""

    documents: list[ExtractionDocument] = []

    for record in list_parsed_documents(
        database_path,
        fund_id=stable_fund_id(fund),
    ):
        try:
            parsed = load_parsed_document(Path(record.text_path))
        except DocumentParseError:
            continue

        documents.append(
            ExtractionDocument(
                record=record,
                document=parsed,
            )
        )

    return documents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reextract_offline",
        description=(
            "Apply the current extraction rules to the documents of a "
            "previous run, without any network access."
        ),
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/funds.json"),
        help="Fund list the previous run was started with.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("cache/regen.sqlite3"),
        help="Processing database holding the parsed documents.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/output/funds.reextracted.json"),
        help="Where to write the regenerated output.",
    )
    parser.add_argument(
        "--fund-id",
        action="append",
        default=None,
        dest="fund_ids",
        help=(
            "Restrict the run to the given fund identifier. May be "
            "repeated. Without it every fund of the input is processed."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    input_path = args.input.resolve()

    database_path = args.database.resolve()

    output_path = args.output.resolve()

    if not database_path.exists():
        print(
            f"Processing database does not exist: {database_path}",
            file=sys.stderr,
        )

        return 1

    try:
        funds = load_funds(input_path)
    except InputFileError as exc:
        print(
            f"Input could not be loaded: {exc}",
            file=sys.stderr,
        )

        return 1

    selected: set[str] | None = set(args.fund_ids) if args.fund_ids else None

    if selected is not None:
        funds = [fund for fund in funds if stable_fund_id(fund) in selected]

        if not funds:
            print(
                "None of the requested fund identifiers is present in the input.",
                file=sys.stderr,
            )

            return 1

    outputs = []

    failures: list[str] = []

    now = datetime.now(UTC)

    for index, fund in enumerate(funds, start=1):
        pending = create_pending_fund(
            fund,
            now=now,
        )

        try:
            documents = _load_documents(
                database_path=database_path,
                fund=fund,
            )
        except DatabaseError as exc:
            failures.append(f"{fund.name}: {type(exc).__name__}: {exc}")

            outputs.append(pending)

            continue

        extracted = extract_fund_fields(
            fund_name=fund.name,
            documents=documents,
        )

        extended = extract_extended_fields(
            fund_name=fund.name,
            fund_web=fund.web,
            documents=documents,
        )

        found_fields = sum(
            1 for field in FIELD_NAMES if getattr(extracted, field).status is FieldStatus.FOUND
        )

        outputs.append(
            pending.model_copy(
                update={
                    "investment_horizon": extracted.investment_horizon,
                    "minimum_investment": extracted.minimum_investment,
                    "target_return": extracted.target_return,
                    "fees": extracted.fees,
                    "assets_under_management": (extracted.assets_under_management),
                    "manager": extended.manager,
                    "administrator": extended.administrator,
                    "aum_history": extended.aum_history,
                    "annual_returns": extended.annual_returns,
                    "historical_values": (extended.historical_values),
                    "news": extended.news,
                    "processing": ProcessingMetadata(
                        status=(
                            ProcessingStatus.COMPLETED
                            if found_fields == len(FIELD_NAMES)
                            else ProcessingStatus.PARTIAL
                        ),
                        updated_at=now,
                        warnings=[],
                    ),
                }
            )
        )

        if index % 50 == 0 or index == len(funds):
            print(
                f"  re-extracted {index}/{len(funds)}",
                flush=True,
            )

    write_output(
        output_path,
        outputs,
        overwrite=True,
    )

    found = sum(
        1
        for output in outputs
        for field in FIELD_NAMES
        if getattr(output, field).status is FieldStatus.FOUND
    )

    ambiguous = sum(
        1
        for output in outputs
        for field in FIELD_NAMES
        if getattr(output, field).status is FieldStatus.AMBIGUOUS
    )

    print()
    print(f"Funds re-extracted : {len(outputs)}")
    print(f"Fields found       : {found}/{len(outputs) * len(FIELD_NAMES)}")
    print(f"Fields ambiguous   : {ambiguous}")
    print(f"Extraction errors  : {len(failures)}")

    print()
    print("EXTENDED FIELDS (schema version 3)")
    print("-" * 62)

    for field in EXTENDED_FIELD_NAMES:
        counts: dict[str, int] = {}

        for output in outputs:
            status = getattr(output, field).status.value

            counts[status] = counts.get(status, 0) + 1

        summary = "  ".join(f"{status}={count}" for status, count in sorted(counts.items()))

        print(f"  {field:<20}{summary}")

    for failure in failures[:10]:
        print(f"  {failure}")

    print()
    print(f"Regenerated output : {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
