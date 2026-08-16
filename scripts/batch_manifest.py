"""
Phases 4 and 9 of the 8-fund crawler recovery experiment.

Reads one isolated batch run and writes the acquisition manifest - every
source the crawler acquired, with where it came from - and the navigation
picture behind it: what the discovery saw, what it followed, and what it
refused and why.

Nothing here fetches anything. It reads the sample database and the files
the crawler already stored.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_selection import resolve  # noqa: E402

from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.output_service import stable_fund_id  # noqa: E402

# Wording that makes a page worth an investor's attention. Used only to
# describe what was acquired; nothing here reads a business value.
RELEVANCE_SIGNALS = (
    "pro investory",
    "pro-investory",
    "for investors",
    "investor",
    "dokument",
    "document",
    "ke stazeni",
    "download",
    "povinne informace",
    "povinne uverejnovane",
    "o fondu",
    "about the fund",
    "parametry",
    "minimalni investice",
    "minimum investment",
    "investicni horizont",
    "investment horizon",
    "cilovy vynos",
    "target return",
    "poplat",
    "fee",
    "fondovy kapital",
    "hodnota majetku",
    "net assets",
    "nav",
    "vykonnost",
    "performance",
    "obhospodarovatel",
    "administrator",
    "aktuality",
    "news",
)


def source_kind(content_type: str | None, url: str) -> str:
    lowered = (content_type or "").casefold()

    if "pdf" in lowered:
        return "PDF"

    if "html" in lowered or "xhtml" in lowered:
        return "HTML"

    if "xml" in lowered:
        return "XML"

    suffix = Path(urlsplit(url).path).suffix.casefold()

    if suffix == ".pdf":
        return "PDF"

    if suffix in {".xlsx", ".xls"}:
        return "SPREADSHEET"

    return "HTML" if not suffix else suffix.lstrip(".").upper()


def signals_of(text: str) -> list[str]:
    lowered = text.casefold()

    return [signal for signal in RELEVANCE_SIGNALS if signal in lowered]


def build(batch_name: str, generation: str) -> dict[str, object]:
    batch = resolve(batch_name)

    database_path = (
        batch.root
        / generation
        / ("sample8.sqlite3" if batch.name == "sample8" else "batch.sqlite3")
    )

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    funds = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    by_name = {fund.name: fund for fund in funds}

    fund_rows: list[dict[str, object]] = []

    for name in batch.fund_names:
        fund = by_name[name]

        fund_id = stable_fund_id(fund)

        referrers = {
            url: discovered_from
            for url, discovered_from in connection.execute(
                "SELECT url, discovered_from FROM discovery_log WHERE fund_id = ?",
                (fund_id,),
            )
        }

        sources: list[dict[str, object]] = []

        for row in connection.execute(
            """
            SELECT
                s.url, s.canonical_url, s.document_type, s.content_type, s.title,
                s.retrieved_at, s.http_status, s.sha256, s.local_path, s.status,
                p.character_count, p.text_path, p.page_count
            FROM sources AS s
            LEFT JOIN parsed_documents AS p ON p.source_id = s.source_id
            WHERE s.fund_id = ? AND s.status IN ('downloaded', 'parsed')
            ORDER BY s.source_id
            """,
            (fund_id,),
        ):
            (
                url,
                canonical,
                document_type,
                content_type,
                title,
                retrieved_at,
                http_status,
                sha256,
                local_path,
                status,
                character_count,
                text_path,
                page_count,
            ) = row

            sources.append(
                {
                    "fund_id": fund_id,
                    "fund_name": name,
                    "canonical_input_url": fund.web,
                    "source_url": url,
                    "final_url": canonical,
                    "referrer_url": referrers.get(url),
                    "source_type": source_kind(content_type, url),
                    "content_type": content_type,
                    "title": title,
                    "document_type": document_type,
                    "local_path": local_path,
                    "sha256": sha256,
                    "retrieved_at": retrieved_at,
                    "http_status": http_status,
                    "status": status,
                    "parsed_characters": character_count,
                    "parsed_pages": page_count,
                    "parsed_path": text_path,
                    # Identity evidence available before anything was read:
                    # whether the address itself carries the fund's own host.
                    "same_host_as_input": (
                        urlsplit(url).hostname == urlsplit(fund.web or "").hostname
                    ),
                    "relevance_signals": signals_of(f"{url} {title or ''}"),
                }
            )

        entries = connection.execute(
            """
            SELECT url, discovered_from, method, priority_score, accepted,
                   rejection_reason, is_document, title
            FROM discovery_log
            WHERE fund_id = ?
            """,
            (fund_id,),
        ).fetchall()

        followed = {entry[0] for entry in entries if entry[4]}

        refused = [
            {
                "url": entry[0],
                "referrer_url": entry[1],
                "method": entry[2],
                "priority_score": entry[3],
                "rejection_reason": entry[5],
                "is_document": bool(entry[6]),
                "title": entry[7],
                "relevance_signals": signals_of(f"{entry[0]} {entry[7] or ''}"),
            }
            for entry in entries
            if not entry[4]
        ]

        # A refused page that reads like an investor section is the one
        # worth a human's attention: the crawler saw the link and did not
        # take it.
        relevant_refusals = [
            item for item in refused if item["relevance_signals"] and not item["is_document"]
        ]

        fund_rows.append(
            {
                "fund_name": name,
                "fund_id": fund_id,
                "canonical_input_url": fund.web,
                "sources_acquired": len(sources),
                "html_acquired": sum(1 for item in sources if item["source_type"] == "HTML"),
                "documents_acquired": sum(1 for item in sources if item["source_type"] != "HTML"),
                "discovery_entries": len(entries),
                "discovery_followed": len(followed),
                "discovery_refused": len(refused),
                "refusal_reasons": dict(
                    Counter(
                        str(item["rejection_reason"] or "unknown") for item in refused
                    ).most_common()
                ),
                "relevant_pages_refused": relevant_refusals[:40],
                "relevant_pages_refused_total": len(relevant_refusals),
                "sources": sources,
            }
        )

    connection.close()

    return {
        "batch": batch.name,
        "generation": generation,
        "database": str(database_path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "sources_total": sum(int(row["sources_acquired"]) for row in fund_rows),
        "funds": fund_rows,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    batch_name = sys.argv[1] if len(sys.argv) > 1 else "sample8"

    generation = sys.argv[2] if len(sys.argv) > 2 else "before"

    payload = build(batch_name, generation)

    path = REPOSITORY_ROOT / f"reports/{batch_name}-acquisition-manifest-{generation}.json"

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"written {path}")

    for row in payload["funds"]:  # type: ignore[index]
        print(
            f"{row['fund_name'][:44]:44} "
            f"sources={row['sources_acquired']:3} "
            f"html={row['html_acquired']:3} docs={row['documents_acquired']:3} "
            f"seen={row['discovery_entries']:4} followed={row['discovery_followed']:3} "
            f"refused-relevant={row['relevant_pages_refused_total']:3}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
