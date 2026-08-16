"""
Re-run the current extractor over an already acquired corpus, offline.

Nothing is fetched and nothing is written into the corpus. The database is
opened read-only, the stored parses are read from disk, and the pages that the
HTML table fix changed are re-parsed in memory so the grids they publish reach
the extractors. PDFs keep their stored parse, because no retained fix touches
the PDF path and re-reading thousands of them would cost hours for nothing.

The result is written as JSON lines, one fund per line, so an interrupted run
keeps everything it has already done.

Usage::

    uv run python scripts/offline_reextract.py \\
        --database cache/regen.sqlite3 \\
        --output cache/offline-recovery1/extraction.jsonl \\
        [--isin-register cache/isin-recovery/isin-hits.json] \\
        [--funds "Name A" --funds "Name B"] [--resume]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from time import perf_counter

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]

sys.path.insert(0, str(SCRIPT_DIRECTORY))

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_selection import DELIVERY_FIELDS  # noqa: E402

from fundscraper.database import ParsedDocumentRecord  # noqa: E402
from fundscraper.document_parser import (  # noqa: E402
    DocumentParseError,
    ParsedDocument,
    load_parsed_document,
    parse_document,
)
from fundscraper.extended_extraction import extract_extended_fields  # noqa: E402
from fundscraper.field_extraction import (  # noqa: E402
    ExtractionDocument,
    IsinIdentity,
    SourceScope,
    extract_fund_fields,
    official_isin_identity,
)
from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.models import FundInput  # noqa: E402
from fundscraper.output_service import stable_fund_id  # noqa: E402

# Formats whose reading changed with the HTML table fix. Everything else keeps
# the parse already on disk.
REPARSED_FORMATS = frozenset({"html", "xhtml", "xml"})


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="offline_reextract")

    parser.add_argument("--database", type=Path, required=True)

    parser.add_argument("--output", type=Path, required=True)

    parser.add_argument("--input", type=Path, default=REPOSITORY_ROOT / "data/input/funds.json")

    parser.add_argument(
        "--isin-register",
        type=Path,
        default=None,
        help="isin-hits.json holding the official ISIN owners.",
    )

    parser.add_argument(
        "--funds",
        action="append",
        default=None,
        help="Restrict the run to these canonical fund names.",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip funds already present in the output file.",
    )

    return parser.parse_args()


def load_isin_register(path: Path | None) -> dict[str, IsinIdentity]:
    """
    Rebuild the official ISIN register the delivered generation was made with.

    The register is a statement of ownership taken from the regulator's list,
    so it is read as it stands; the tier each entry was matched at is carried
    through to the report rather than used to drop entries here.
    """

    if path is None:
        return {}

    payload = json.loads(path.read_text(encoding="utf-8"))

    meta = payload.get("isin_meta") or {}

    register: dict[str, IsinIdentity] = {}

    for isin, entry in meta.items():
        fund_name = entry.get("canonical_fund")

        if not fund_name:
            continue

        register[isin] = IsinIdentity(
            fund_name=fund_name,
            scope=SourceScope.SUBFUND if entry.get("is_subfund") else SourceScope.EXACT_FUND,
        )

    return register


def read_documents(
    *,
    connection: sqlite3.Connection,
    fund_id: str,
) -> tuple[list[ExtractionDocument], int, int]:
    """Return the corpus of one fund, with the pages re-read where it matters."""

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

    documents: list[ExtractionDocument] = []

    reparsed = 0

    tables = 0

    for row in rows:
        record = ParsedDocumentRecord(
            source_id=row[0],
            fund_id=row[1],
            url=row[2],
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

        parsed: ParsedDocument | None = None

        if record.document_format in REPARSED_FORMATS and record.local_path:
            body_path = Path(record.local_path)

            if body_path.exists():
                try:
                    parsed = parse_document(
                        body=body_path.read_bytes(),
                        content_type=record.content_type,
                        url=record.url,
                    )

                    reparsed += 1

                    tables += parsed.table_count
                except (DocumentParseError, OSError, ValueError):
                    parsed = None

        if parsed is None:
            try:
                parsed = load_parsed_document(Path(record.text_path))
            except DocumentParseError:
                continue

        documents.append(ExtractionDocument(record=record, document=parsed))

    return (documents, reparsed, tables)


def field_payload(result: object) -> dict[str, object]:
    """Flatten one field result into what the delta comparison needs."""

    status = getattr(result, "status", None)

    value = getattr(result, "value", None)

    source = getattr(result, "source", None)

    holder = getattr(source, "source", None) if source is not None else None

    reason = getattr(result, "reason", None)

    scope = getattr(result, "scope", None)

    return {
        "status": getattr(status, "value", None),
        "value": value.model_dump(mode="json") if value is not None else None,
        "source_url": (str(getattr(holder, "url", "")) or None) if holder is not None else None,
        "document_type": getattr(holder, "document_type", None)
        and getattr(getattr(holder, "document_type", None), "value", None),
        "page": getattr(source, "page_number", None) if source is not None else None,
        "quote": getattr(source, "quote", None) if source is not None else None,
        "scope": scope.model_dump(mode="json") if scope is not None else None,
        "reason_code": getattr(getattr(reason, "code", None), "value", None),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    arguments = parse_arguments()

    funds: list[FundInput] = load_funds(arguments.input)

    if arguments.funds:
        wanted = set(arguments.funds)

        funds = [fund for fund in funds if fund.name in wanted]

    arguments.output.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()

    if arguments.resume and arguments.output.exists():
        for line in arguments.output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["name"])

        print(f"resuming, {len(done)} funds already extracted", flush=True)

    register = load_isin_register(arguments.isin_register)

    print(
        f"database={arguments.database} funds={len(funds)} isin_register={len(register)}",
        flush=True,
    )

    connection = sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True)

    started = perf_counter()

    with arguments.output.open("a", encoding="utf-8") as handle:
        for index, fund in enumerate(funds, start=1):
            if fund.name in done:
                continue

            fund_started = perf_counter()

            documents, reparsed, tables = read_documents(
                connection=connection,
                fund_id=stable_fund_id(fund),
            )

            with official_isin_identity(register or None):
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

            fields = {
                name: field_payload(getattr(core, name, None) or getattr(extended, name))
                for name in DELIVERY_FIELDS
            }

            found = sum(1 for item in fields.values() if item["status"] == "found")

            handle.write(
                json.dumps(
                    {
                        "name": fund.name,
                        "web": fund.web,
                        "fund_id": stable_fund_id(fund),
                        "documents": len(documents),
                        "markup_reparsed": reparsed,
                        "markup_tables": tables,
                        "found": found,
                        "fields": fields,
                        "seconds": round(perf_counter() - fund_started, 2),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

            handle.flush()

            print(
                f"[{index}/{len(funds)}] {fund.name[:44]:46} "
                f"docs={len(documents):3} tables={tables:4} found={found:2} "
                f"{round(perf_counter() - fund_started, 1)}s",
                flush=True,
            )

    connection.close()

    print(f"\nfinished in {round(perf_counter() - started, 1)}s -> {arguments.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
