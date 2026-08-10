"""
Re-parse downloaded documents with the current parser.

The parsed cache was written before the parser preserved text blocks and
tables, so the files in it carry text alone. Extraction that reads a
table can only do so once the cache holds one, which is what this script
rebuilds.

Nothing is downloaded. Every document is read from the copy the earlier
run already saved, so the script performs no network access.

Usage:

    uv run python scripts/regenerate_parsed_cache.py \\
        --database cache/regen.sqlite3 \\
        --parsed-directory cache/parsed
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from fundscraper.database import (
    DatabaseError,
    record_parsed_document,
)
from fundscraper.document_parser import (
    DocumentParseError,
    parse_document,
    write_parsed_document,
)


@dataclass(frozen=True, slots=True)
class SourceToParse:
    source_id: int
    fund_id: str
    url: str
    content_type: str | None
    local_path: str


def sources_to_parse(
    *,
    database_path: Path,
    only_missing_tables: bool,
    parsed_directory: Path,
    limit: int,
) -> list[SourceToParse]:
    """Return the downloaded documents that still need re-parsing."""

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT source_id, fund_id, url, content_type, local_path
            FROM sources
            WHERE local_path IS NOT NULL AND status = 'downloaded'
            ORDER BY source_id
            """,
        ).fetchall()

    pending: list[SourceToParse] = []

    for source_id, fund_id, url, content_type, local_path in rows:
        if only_missing_tables and _already_structured(
            parsed_directory=parsed_directory,
            fund_id=str(fund_id),
            source_id=int(source_id),
        ):
            continue

        pending.append(
            SourceToParse(
                source_id=int(source_id),
                fund_id=str(fund_id),
                url=str(url),
                content_type=content_type,
                local_path=str(local_path),
            )
        )

        if limit and len(pending) >= limit:
            break

    return pending


def _already_structured(
    *,
    parsed_directory: Path,
    fund_id: str,
    source_id: int,
) -> bool:
    """Return whether a parsed file already carries the new structure."""

    path = parsed_directory / fund_id / f"{source_id}.json"

    if not path.exists():
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    pages = payload.get("pages")

    if not isinstance(pages, list):
        return False

    # A markup page has neither blocks nor tables and never will, so it
    # counts as done once it has been parsed at all.
    if str(payload.get("document_format")) != "pdf":
        return True

    return any("blocks" in page or "tables" in page for page in pages if isinstance(page, dict))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="regenerate_parsed_cache",
        description=("Re-parse downloaded documents so tables become available."),
    )

    parser.add_argument("--database", type=Path, default=Path("cache/regen.sqlite3"))
    parser.add_argument("--parsed-directory", type=Path, default=Path("cache/parsed"))
    parser.add_argument("--report", type=Path, default=Path("reports/parsed-cache-refresh.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-parse every document, not only those without structure.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    database_path = args.database.resolve()

    parsed_directory = args.parsed_directory.resolve()

    if not database_path.exists():
        print(f"Database does not exist: {database_path}", file=sys.stderr)

        return 1

    pending = sources_to_parse(
        database_path=database_path,
        only_missing_tables=not args.all,
        parsed_directory=parsed_directory,
        limit=args.limit,
    )

    print(f"Documents to re-parse : {len(pending)}")

    parsed = 0
    failed = 0
    with_tables = 0
    total_tables = 0
    total_blocks = 0

    parsers: Counter[str] = Counter()

    failures: list[dict[str, str]] = []

    for index, source in enumerate(pending, start=1):
        path = Path(source.local_path)

        if not path.exists():
            failed += 1

            failures.append({"url": source.url, "error": "downloaded file is missing"})

            continue

        try:
            document = parse_document(
                body=path.read_bytes(),
                content_type=source.content_type,
                url=source.url,
            )

            parsed_path = write_parsed_document(
                directory=parsed_directory,
                fund_id=source.fund_id,
                source_id=source.source_id,
                document=document,
            )

            record_parsed_document(
                database_path,
                source_id=source.source_id,
                fund_id=source.fund_id,
                document_format=document.document_format.value,
                parser_name=document.parser_name,
                page_count=document.page_count,
                character_count=document.character_count,
                scanned_candidate=document.scanned_candidate,
                text_path=str(parsed_path),
            )
        except (DocumentParseError, DatabaseError, OSError, ValueError) as exc:
            failed += 1

            failures.append(
                {
                    "url": source.url,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

            continue

        parsed += 1

        parsers[document.parser_name] += 1

        tables = document.table_count

        total_tables += tables

        total_blocks += sum(len(page.blocks) for page in document.pages)

        if tables:
            with_tables += 1

        if index % 100 == 0:
            print(f"  re-parsed {index}/{len(pending)}", flush=True)

    print()
    print(f"Documents re-parsed    : {parsed}")
    print(f"Failures               : {failed}")
    print(f"Documents with tables  : {with_tables}")
    print(f"Tables extracted       : {total_tables}")
    print(f"Text blocks extracted  : {total_blocks}")
    print()
    print("Parsers used:")

    for name, count in parsers.most_common():
        print(f"  {name:<24}{count}")

    report_path = args.report.resolve()

    report_path.parent.mkdir(parents=True, exist_ok=True)

    report_path.write_text(
        json.dumps(
            {
                "documents_considered": len(pending),
                "documents_reparsed": parsed,
                "failures": failed,
                "documents_with_tables": with_tables,
                "tables_extracted": total_tables,
                "text_blocks_extracted": total_blocks,
                "parsers": dict(sorted(parsers.items())),
                "failure_details": failures[:100],
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


if __name__ == "__main__":
    raise SystemExit(main())
