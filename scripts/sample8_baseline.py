"""
Phase 1 of the 8-fund crawler recovery experiment: the current delivery state.

Reads the newest delivery generation and the production database read-only and
writes ``reports/sample8-before.{json,md}``. Nothing here mutates any
production artefact.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.output_service import stable_fund_id  # noqa: E402

SAMPLE_FUND_NAMES = (
    "3M FUND MSI SICAV a.s.",
    "CARE SICAV, a.s.",
    "DOMOPLAN - Projekty Brno SICAV, a.s.",
    "KOOR ESG SICAV a.s.",
    "Lázeňský fond SICAV a.s.",
    "MINT rezidenční fond SICAV, a.s.",
    "Natland Real Estate SICAV, a.s.",
    "Nemomax investiční fond s proměnným základním kapitálem, a.s.",
)


DELIVERY_FIELDS = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
)


USABLE_STATUSES = frozenset({"VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM"})


def html_like(content_type: str | None, url: str) -> bool:
    if content_type and "html" in content_type.casefold():
        return True

    if content_type and "pdf" in content_type.casefold():
        return False

    return not url.casefold().endswith((".pdf", ".xlsx", ".xls", ".doc", ".docx"))


def main() -> int:
    enriched_path = REPOSITORY_ROOT / "data/output/funds.delivery.autonomous4-enriched.json"

    database_path = REPOSITORY_ROOT / "cache/regen.sqlite3"

    funds = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    by_name = {fund.name: fund for fund in funds}

    enriched = json.loads(enriched_path.read_text(encoding="utf-8"))

    enriched_by_name = {record["name"]: record for record in enriched}

    # The generation the operator was reading when the sample was chosen.
    # It predates three recovery checkpoints, so it is reported next to
    # the current one rather than instead of it.
    stale_path = REPOSITORY_ROOT / "data/output/funds.delivery.safe.json"

    stale_by_name = {
        record["name"]: record
        for record in json.loads(stale_path.read_text(encoding="utf-8"))
    }

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    rows: list[dict[str, object]] = []

    for name in SAMPLE_FUND_NAMES:
        fund = by_name[name]

        fund_id = stable_fund_id(fund)

        record = enriched_by_name[name]

        field_state: dict[str, str] = {}

        for field in DELIVERY_FIELDS:
            payload = record.get(field)

            status = (payload or {}).get("status") if isinstance(payload, dict) else None

            field_state[field] = status or "NOT_FOUND"

        usable = [field for field, status in field_state.items() if status in USABLE_STATUSES]

        verified = [field for field, status in field_state.items() if status == "VERIFIED"]

        provisional = [
            field
            for field, status in field_state.items()
            if status in {"PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM"}
        ]

        missing = [field for field in DELIVERY_FIELDS if field not in usable]

        # A source that was read moves on to ``parsed``; both states mean
        # the bytes are on disk, so counting only ``downloaded`` would
        # report zero retained pages for every fund that finished.
        source_rows = connection.execute(
            """
            SELECT url, content_type, status, document_type
            FROM sources
            WHERE fund_id = ? AND status IN ('downloaded', 'parsed')
            """,
            (fund_id,),
        ).fetchall()

        html_count = sum(
            1 for url, content_type, _, _ in source_rows if html_like(content_type, url)
        )

        document_count = len(source_rows) - html_count

        parsed_count = connection.execute(
            "SELECT COUNT(*) FROM parsed_documents WHERE fund_id = ?",
            (fund_id,),
        ).fetchone()[0]

        accepted_source_urls = {
            (record.get(field) or {}).get("source_url")
            for field in DELIVERY_FIELDS
            if isinstance(record.get(field), dict)
        }

        accepted_source_urls.discard(None)

        stale_record = stale_by_name.get(name, {})

        stale_usable = sum(
            1
            for field in DELIVERY_FIELDS
            if isinstance(stale_record.get(field), dict)
            and stale_record[field].get("status") == "found"
        )

        rows.append(
            {
                "name": name,
                "fund_id": fund_id,
                "input_web": fund.web,
                "usable_fields_stale_safe_generation": stale_usable,
                "usable_fields": len(usable),
                "verified": len(verified),
                "provisional": len(provisional),
                "usable_field_names": usable,
                "missing_fields": missing,
                "accepted_source_count": len(accepted_source_urls),
                "accepted_source_urls": sorted(accepted_source_urls),
                "downloaded_sources": len(source_rows),
                "html_sources": html_count,
                "document_sources": document_count,
                "parsed_documents": parsed_count,
                "field_status": field_state,
            }
        )

    connection.close()

    payload = {
        "generated_from": str(enriched_path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "database": "cache/regen.sqlite3 (read-only)",
        "fields_per_fund": len(DELIVERY_FIELDS),
        "sample_size": len(rows),
        "theoretical_slots": len(rows) * len(DELIVERY_FIELDS),
        "usable_total": sum(int(row["usable_fields"]) for row in rows),
        "verified_total": sum(int(row["verified"]) for row in rows),
        "funds": rows,
    }

    report_directory = REPOSITORY_ROOT / "reports"

    (report_directory / "sample8-before.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Sample 8 — delivery state before the crawler recovery run",
        "",
        f"Source: `{payload['generated_from']}`",
        "",
        f"Slots: {payload['theoretical_slots']} · usable {payload['usable_total']} "
        f"({payload['usable_total'] / payload['theoretical_slots']:.1%}) · "
        f"VERIFIED {payload['verified_total']}",
        "",
        "| fund | input web | usable | VERIFIED | provisional | stale safe gen. "
        "| accepted sources | HTML | docs | parsed |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for row in rows:
        lines.append(
            f"| {row['name']} | `{row['input_web']}` | {row['usable_fields']}/11 | "
            f"{row['verified']} | {row['provisional']} | "
            f"{row['usable_fields_stale_safe_generation']}/11 | "
            f"{row['accepted_source_count']} | "
            f"{row['html_sources']} | {row['document_sources']} | {row['parsed_documents']} |"
        )

    lines.extend(["", "## Missing fields", ""])

    for row in rows:
        missing_fields = row["missing_fields"]

        assert isinstance(missing_fields, list)

        lines.append(f"- **{row['name']}** — {', '.join(missing_fields) or 'none'}")

    (report_directory / "sample8-before.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(payload["funds"], ensure_ascii=False, indent=2)[:200])

    print(
        f"usable {payload['usable_total']}/{payload['theoretical_slots']} "
        f"verified {payload['verified_total']}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
