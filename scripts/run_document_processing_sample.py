"""
Re-process cached documents and report what was learned about them.

The script performs no network access. It re-parses documents a previous
run downloaded, classifies each one, decides which fund it belongs to and
reads the dates it carries, then reports the result next to the document
type the earlier run had recorded.

Nothing is written to the original processing database; the metadata goes
to a separate one, so the earlier run stays intact.

Usage:

    uv run python scripts/run_document_processing_sample.py \\
        --baseline cache/regen.sqlite3 \\
        --database cache/step6-documents.sqlite3 \\
        --report reports/step6-document-sample.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fundscraper.database import (
    DatabaseError,
    ParsedDocumentRecord,
    initialize_database,
    register_funds,
)
from fundscraper.document_metadata import describe_document, store_document_facts
from fundscraper.document_parser import DocumentParseError, parse_document
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id


def load_rows(
    *,
    baseline: Path,
    limit: int,
    source_ids: tuple[int, ...],
) -> list[dict[str, Any]]:
    """Return the cached documents to re-process."""

    with sqlite3.connect(baseline) as connection:
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)

            statement = (
                "SELECT s.source_id, s.url, s.content_type, s.local_path, "
                "s.document_type, f.name, f.web "
                "FROM sources s JOIN funds f ON f.fund_id = s.fund_id "
                f"WHERE s.source_id IN ({placeholders}) AND s.local_path IS NOT NULL"
            )

            rows = connection.execute(
                statement,
                source_ids,
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT s.source_id, s.url, s.content_type, s.local_path, "
                "s.document_type, f.name, f.web "
                "FROM sources s JOIN funds f ON f.fund_id = s.fund_id "
                "WHERE s.local_path IS NOT NULL AND s.content_type LIKE '%pdf%' "
                "ORDER BY s.source_id LIMIT ?",
                (limit,),
            ).fetchall()

    return [
        {
            "source_id": int(row[0]),
            "url": str(row[1]),
            "content_type": row[2],
            "local_path": str(row[3]),
            "previous_type": row[4],
            "fund_name": str(row[5]),
            "fund_web": str(row[6]),
        }
        for row in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_document_processing_sample",
        description=("Re-parse cached documents and report type, scope and dates."),
    )

    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("cache/regen.sqlite3"),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("cache/step6-documents.sqlite3"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/step6-document-sample.json"),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
    )
    parser.add_argument(
        "--source-id",
        action="append",
        type=int,
        default=None,
        dest="source_ids",
        help="Re-process only these cached sources. May be repeated.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    rows = load_rows(
        baseline=args.baseline.resolve(),
        limit=args.limit,
        source_ids=tuple(args.source_ids or ()),
    )

    if not rows:
        print(
            "No cached documents matched the request.",
            file=sys.stderr,
        )

        return 1

    database_path = args.database.resolve()

    initialize_database(database_path)

    funds = {
        row["fund_name"]: FundInput(
            name=row["fund_name"],
            web=row["fund_web"],
        )
        for row in rows
    }

    register_funds(
        database_path,
        list(funds.values()),
    )

    results: list[dict[str, Any]] = []

    types: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    parsers: Counter[str] = Counter()
    failures = 0
    changed_type = 0

    for index, row in enumerate(
        rows,
        start=1,
    ):
        path = Path(row["local_path"])

        if not path.exists():
            continue

        fund = funds[row["fund_name"]]

        try:
            document = parse_document(
                body=path.read_bytes(),
                content_type=row["content_type"],
                url=row["url"],
            )
        except (DocumentParseError, OSError) as exc:
            failures += 1

            results.append(
                {
                    "source_id": row["source_id"],
                    "url": row["url"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

            continue

        facts = describe_document(
            record=ParsedDocumentRecord(
                source_id=row["source_id"],
                fund_id=stable_fund_id(fund),
                url=row["url"],
                title=None,
                document_type=row["previous_type"],
                content_type=row["content_type"],
                retrieved_at=None,
                document_format=document.document_format.value,
                parser_name=document.parser_name,
                page_count=document.page_count,
                character_count=document.character_count,
                scanned_candidate=document.scanned_candidate,
                text_path="",
                parsed_at="",
                local_path=row["local_path"],
            ),
            document=document,
            fund_name=fund.name,
        )

        try:
            store_document_facts(
                database_path=database_path,
                fund_id=stable_fund_id(fund),
                facts=facts,
            )
        except DatabaseError as exc:
            print(
                f"  could not store {row['source_id']}: {exc}",
                file=sys.stderr,
            )

        new_type = facts.classification.document_type.value

        types[new_type] += 1
        scopes[facts.identity.scope.value] += 1
        parsers[facts.parser_name] += 1

        if new_type != (row["previous_type"] or "other"):
            changed_type += 1

        results.append(
            {
                "source_id": row["source_id"],
                "url": row["url"],
                "fund": fund.name,
                "previous_type": row["previous_type"],
                "document_type": new_type,
                "type_score": facts.classification.score,
                "type_ambiguous": (facts.classification.ambiguous),
                "scope": facts.identity.scope.value,
                "scope_confidence": (facts.identity.confidence),
                "scope_accepted": (facts.identity.is_accepted),
                "subfund": facts.identity.subfund_name,
                "share_class": facts.identity.share_class,
                "published_at": _date(facts.dates.published_at),
                "published_origin": (
                    facts.dates.published_at.origin.value if facts.dates.published_at else None
                ),
                "effective_at": _date(facts.dates.effective_at),
                "reporting_period_end": (_date(facts.dates.reporting_period_end)),
                "as_of": _date(facts.dates.as_of),
                "parser": facts.parser_name,
                "pages": facts.pages_parsed,
                "tables": document.table_count,
                "blocks": sum(len(page.blocks) for page in document.pages),
                "scanned_candidate": (document.scanned_candidate),
                "warnings": list(facts.warnings),
            }
        )

        if index % 50 == 0:
            print(
                f"  processed {index}/{len(rows)}",
                flush=True,
            )

    processed = len(results) - failures

    print()
    print(f"documents re-processed : {processed}")
    print(f"parse failures         : {failures}")
    print(f"type changed vs before : {changed_type}")
    print()
    print("document types:")

    for name, count in types.most_common():
        print(f"  {name:<24}{count}")

    print()
    print("scope:")

    for name, count in scopes.most_common():
        print(f"  {name:<24}{count}")

    print()
    print("parsers:")

    for name, count in parsers.most_common():
        print(f"  {name:<24}{count}")

    with_published = sum(1 for item in results if item.get("published_at"))
    with_effective = sum(1 for item in results if item.get("effective_at"))
    with_period = sum(1 for item in results if item.get("reporting_period_end"))

    print()
    print(f"with publication date  : {with_published}")
    print(f"with effective date    : {with_effective}")
    print(f"with reporting period  : {with_period}")

    tables = sum(int(item.get("tables") or 0) for item in results)

    with_tables = sum(1 for item in results if item.get("tables"))

    print(f"tables extracted       : {tables} across {with_tables} documents")

    report_path = args.report.resolve()

    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_path.write_text(
        json.dumps(
            {
                "documents": results,
                "document_types": dict(sorted(types.items())),
                "scopes": dict(sorted(scopes.items())),
                "parsers": dict(sorted(parsers.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(f"Full report: {report_path}")

    return 0


def _date(
    value: object,
) -> str | None:
    if value is None:
        return None

    return str(getattr(value, "value", "")) or None


if __name__ == "__main__":
    raise SystemExit(main())
