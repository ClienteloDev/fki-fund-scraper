"""
Run the deterministic extractors over a chosen set of parsed sources.

The pipeline reports a field as missing without saying which document it
read and what it made of it. This probe puts one or more parsed documents
in front of exactly the extractors the pipeline uses and prints what came
back, so the stage a value disappeared at can be named.

Usage::

    uv run python scripts/sample8_probe.py <database> <fund name> [url substring ...]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from fundscraper.database import ParsedDocumentRecord  # noqa: E402
from fundscraper.document_parser import load_parsed_document  # noqa: E402
from fundscraper.extended_extraction import extract_extended_fields  # noqa: E402
from fundscraper.field_extraction import (  # noqa: E402
    ExtractionDocument,
    extract_fund_fields,
)
from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.output_service import stable_fund_id  # noqa: E402


def load_documents(
    *,
    database_path: Path,
    fund_id: str,
    url_filters: tuple[str, ...],
) -> list[ExtractionDocument]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    rows = connection.execute(
        """
        SELECT
            p.source_id, p.fund_id, s.url, s.title, s.document_type, s.content_type,
            s.retrieved_at, p.document_format, p.parser_name, p.page_count,
            p.character_count, p.scanned_candidate, p.text_path, p.parsed_at, s.local_path
        FROM parsed_documents AS p
        JOIN sources AS s ON s.source_id = p.source_id
        WHERE p.fund_id = ?
        ORDER BY p.source_id
        """,
        (fund_id,),
    ).fetchall()

    connection.close()

    documents: list[ExtractionDocument] = []

    for row in rows:
        url = row[2]

        if url_filters and not any(needle in url for needle in url_filters):
            continue

        record = ParsedDocumentRecord(
            source_id=row[0],
            fund_id=row[1],
            url=url,
            title=row[3],
            document_type=row[4],
            content_type=row[5],
            retrieved_at=row[6],
            document_format=row[7],
            parser_name=row[8],
            page_count=row[9],
            character_count=row[10],
            scanned_candidate=bool(row[11]),
            text_path=row[12],
            parsed_at=row[13],
            local_path=row[14],
        )

        documents.append(
            ExtractionDocument(
                record=record,
                document=load_parsed_document(Path(record.text_path)),
            )
        )

    return documents


def describe(field_name: str, value: object) -> str:
    payload = getattr(value, "model_dump", lambda **_: value)(mode="json")

    return f"{field_name}: {json.dumps(payload, ensure_ascii=False)}"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    database_path = Path(sys.argv[1])

    fund_name = sys.argv[2]

    url_filters = tuple(sys.argv[3:])

    funds = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    fund = next(item for item in funds if item.name == fund_name)

    documents = load_documents(
        database_path=database_path,
        fund_id=stable_fund_id(fund),
        url_filters=url_filters,
    )

    print(f"{fund.name} — {len(documents)} parsed document(s)")

    for document in documents:
        print(
            f"  #{document.record.source_id} {document.record.document_type} "
            f"{document.record.character_count}c {document.record.url}"
        )

    core = extract_fund_fields(
        fund_name=fund.name,
        documents=documents,
        fund_web=fund.web,
    )

    extended = extract_extended_fields(
        fund_name=fund.name,
        fund_web=fund.web,
        documents=documents,
    )

    print("\n--- core ---")

    for name in (
        "investment_horizon",
        "minimum_investment",
        "target_return",
        "fees",
        "assets_under_management",
    ):
        print(describe(name, getattr(core, name)))

    print("\n--- extended ---")

    for name in (
        "manager",
        "administrator",
        "aum_history",
        "annual_returns",
        "historical_values",
        "news",
    ):
        print(describe(name, getattr(extended, name)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
