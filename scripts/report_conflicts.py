"""
Report every disagreement between the sources of a fund.

The script re-runs the extraction against the parsed cache and records
what the resolver did each time two candidates claimed the same thing:
they agreed after normalization, one of them was clearly stronger, or
nothing separated them and the field kept the conflict.

Nothing is written to the delivered output and no request is made. Only
the report file is produced.

Usage examples:

    uv run python scripts/report_conflicts.py
    uv run python scripts/report_conflicts.py --limit 20
    uv run python scripts/report_conflicts.py \\
        --fund-id fund_93496dfa28691089 --examples 20
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fundscraper.conflict_resolution import (
    ConflictLedger,
    ConflictOutcome,
    ConflictRecord,
)
from fundscraper.database import (
    DatabaseError,
    list_parsed_documents,
)
from fundscraper.document_parser import (
    DocumentParseError,
    load_parsed_document,
)
from fundscraper.extended_extraction import extract_extended_fields
from fundscraper.field_extraction import (
    ExtractionDocument,
    extract_fund_fields,
)
from fundscraper.input_loader import InputFileError, load_funds
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id


def load_documents(
    *,
    database_path: Path,
    fund_id: str,
) -> list[ExtractionDocument]:
    """Load the parsed documents of one fund exactly as extraction does."""

    documents: list[ExtractionDocument] = []

    for record in list_parsed_documents(
        database_path,
        fund_id=fund_id,
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


def inspect_fund(
    *,
    database_path: Path,
    fund: FundInput,
    ledger: ConflictLedger,
) -> int:
    """Run every extractor of one fund and return the documents read."""

    fund_ledger = ledger.for_fund(fund.name)

    documents = load_documents(
        database_path=database_path,
        fund_id=stable_fund_id(fund),
    )

    if not documents:
        return 0

    extract_fund_fields(
        fund_name=fund.name,
        documents=documents,
        fund_web=fund.web,
        ledger=fund_ledger,
    )

    extract_extended_fields(
        fund_name=fund.name,
        fund_web=fund.web,
        documents=documents,
        ledger=fund_ledger,
    )

    return len(documents)


def record_payload(
    record: ConflictRecord,
) -> dict[str, Any]:
    return {
        "fund_name": record.fund_name,
        "field": record.field,
        "semantic_key": record.semantic_key,
        "outcome": record.outcome.value,
        "reason": record.reason,
        "decided_by": record.decided_by,
        "selected": (asdict(record.selected) if record.selected is not None else None),
        "alternatives": [asdict(item) for item in record.alternatives],
        "ranking_factors": [asdict(item) for item in record.factors],
    }


def build_report(
    *,
    ledger: ConflictLedger,
    funds_inspected: int,
    documents_read: int,
) -> dict[str, Any]:
    records = ledger.records

    by_outcome: Counter[str] = Counter(record.outcome.value for record in records)

    by_field: dict[str, Counter[str]] = {}

    by_reason: Counter[str] = Counter()

    by_decided_by: Counter[str] = Counter()

    for record in records:
        by_field.setdefault(
            record.field,
            Counter(),
        )[record.outcome.value] += 1

        by_reason[record.reason] += 1

        # A group whose members agree was never decided, so counting it
        # among the undecided ones would read as a failure to choose.
        if record.outcome is not ConflictOutcome.EQUIVALENT:
            by_decided_by[record.decided_by or "undecided"] += 1

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "funds_inspected": funds_inspected,
            "documents_read": documents_read,
            "conflicts_inspected": len(records),
            "auto_resolved": by_outcome.get(
                ConflictOutcome.AUTO_RESOLVED.value,
                0,
            ),
            "unresolved": by_outcome.get(
                ConflictOutcome.UNRESOLVED.value,
                0,
            ),
            "equivalent_after_normalization": by_outcome.get(
                ConflictOutcome.EQUIVALENT.value,
                0,
            ),
            "by_field": {
                field: dict(sorted(counts.items())) for field, counts in sorted(by_field.items())
            },
            "by_decided_by": dict(sorted(by_decided_by.items(), key=lambda item: -item[1])),
            "by_reason": dict(sorted(by_reason.items(), key=lambda item: -item[1])[:40]),
        },
        "conflicts": [record_payload(record) for record in records],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report_conflicts",
        description=(
            "Re-extract the parsed cache offline and report every "
            "disagreement between the sources of a fund."
        ),
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/funds.json"),
        help="Canonical fund list.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("cache/regen.sqlite3"),
        help="Database holding the parsed documents.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/conflicts.step8.json"),
        help="Where to write the JSON report.",
    )
    parser.add_argument(
        "--fund-id",
        action="append",
        default=[],
        help="Inspect only these funds. May be repeated.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Inspect only the first N funds. 0 means every fund.",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=8,
        help="How many conflicts to print to the console.",
    )

    return parser


def selected_funds(
    *,
    funds: list[FundInput],
    fund_ids: list[str],
    limit: int,
) -> list[FundInput]:
    if fund_ids:
        wanted = set(fund_ids)

        funds = [fund for fund in funds if stable_fund_id(fund) in wanted]

    return funds[:limit] if limit > 0 else funds


def print_summary(
    *,
    report: dict[str, Any],
    examples: int,
) -> None:
    summary = report["summary"]

    print(f"Funds inspected:   {summary['funds_inspected']}")
    print(f"Documents read:    {summary['documents_read']}")
    print(f"Conflicts:         {summary['conflicts_inspected']}")
    print()
    print("OUTCOME")
    print("-" * 62)
    print(f"  {'auto_resolved':<40}{summary['auto_resolved']:>6}")
    print(f"  {'unresolved':<40}{summary['unresolved']:>6}")
    print(f"  {'equivalent_after_normalization':<40}{summary['equivalent_after_normalization']:>6}")

    print()
    print("FIELD")
    print("-" * 62)
    print(f"  {'field':<26}{'resolved':>10}{'unresolved':>12}{'equivalent':>12}")

    for field, counts in summary["by_field"].items():
        print(
            f"  {field:<26}"
            f"{counts.get(ConflictOutcome.AUTO_RESOLVED.value, 0):>10}"
            f"{counts.get(ConflictOutcome.UNRESOLVED.value, 0):>12}"
            f"{counts.get(ConflictOutcome.EQUIVALENT.value, 0):>12}"
        )

    print()
    print("DECIDED BY")
    print("-" * 62)

    for factor, count in summary["by_decided_by"].items():
        print(f"  {factor:<40}{count:>6}")

    if examples <= 0:
        return

    unresolved = [
        item for item in report["conflicts"] if item["outcome"] == ConflictOutcome.UNRESOLVED.value
    ]

    resolved = [
        item
        for item in report["conflicts"]
        if item["outcome"] == ConflictOutcome.AUTO_RESOLVED.value
    ]

    for title, items in (
        ("UNRESOLVED", unresolved),
        ("AUTO-RESOLVED", resolved),
    ):
        if not items:
            continue

        print()
        print(f"{title} EXAMPLES")
        print("-" * 62)

        for item in items[:examples]:
            print(f"  {item['field']} / {item['semantic_key']}")
            print(f"      fund:   {item['fund_name']}")

            if item["selected"]:
                print(
                    f"      kept:   {item['selected']['value']} "
                    f"[{item['selected']['authority']}] "
                    f"{(item['selected']['source_url'] or '')[:70]}"
                )

            for alternative in item["alternatives"][:3]:
                print(
                    f"      other:  {alternative['value']} "
                    f"[{alternative['authority']}] "
                    f"{(alternative['source_url'] or '')[:70]}"
                )

            print(f"      why:    {item['reason']}")
            print()


def main() -> int:
    args = build_parser().parse_args()

    try:
        funds = load_funds(args.input.resolve())
    except InputFileError as exc:
        print(
            f"Input could not be loaded: {exc}",
            file=sys.stderr,
        )

        return 1

    chosen = selected_funds(
        funds=funds,
        fund_ids=list(args.fund_id),
        limit=args.limit,
    )

    ledger = ConflictLedger()

    database_path = args.database.resolve()

    documents_read = 0

    for index, fund in enumerate(
        chosen,
        start=1,
    ):
        try:
            documents_read += inspect_fund(
                database_path=database_path,
                fund=fund,
                ledger=ledger,
            )
        except DatabaseError as exc:
            print(
                f"Fund {fund.name} could not be read: {exc}",
                file=sys.stderr,
            )

            continue

        if index % 10 == 0 or index == len(chosen):
            print(
                f"  [{index}/{len(chosen)}] {len(ledger.records)} conflicts so far",
                flush=True,
            )

    report = build_report(
        ledger=ledger,
        funds_inspected=len(chosen),
        documents_read=documents_read,
    )

    report_path = args.report.resolve()

    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print()

    print_summary(
        report=report,
        examples=args.examples,
    )

    print()
    print(f"Full report: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
