"""
Write a new delivery generation from the audited offline-recovery delta.

The authoritative generation is read, never written. Only the candidates an
audit decision accepted are applied, each one carrying the evidence it was
accepted on, and the canonical 341-fund order is preserved exactly.

The audit file is a list of decisions keyed by fund and field:

    [{"fund": "...", "field": "...", "decision": "VERIFIED", "rationale": "..."}]

Anything the audit does not mention is left as it stands, and a decision of
REJECTED is recorded in the report rather than applied.

Usage::

    uv run python scripts/offline_apply.py \\
        --authoritative data/output/funds.delivery.autonomous4-enriched.json \\
        --delta reports/offline-recovery1-delta.json \\
        --audit reports/offline-recovery1-audit.json \\
        --generation offline-recovery1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]

sys.path.insert(0, str(SCRIPT_DIRECTORY))

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_selection import DELIVERY_FIELDS  # noqa: E402

from fundscraper.input_loader import load_funds  # noqa: E402

APPLIED = ("VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM", "CONFLICTING")

USABLE = ("VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(prog="offline_apply")

    parser.add_argument("--authoritative", type=Path, required=True)

    parser.add_argument("--delta", type=Path, required=True)

    parser.add_argument("--audit", type=Path, required=True)

    parser.add_argument("--generation", required=True)

    arguments = parser.parse_args()

    records = json.loads(arguments.authoritative.read_text(encoding="utf-8"))

    delta = json.loads(arguments.delta.read_text(encoding="utf-8"))

    decisions = json.loads(arguments.audit.read_text(encoding="utf-8"))

    by_key = {(str(item["fund"]), str(item["field"])): item for item in delta["items"]}

    canonical = [fund.name for fund in load_funds(REPOSITORY_ROOT / "data/input/funds.json")]

    if [record.get("name") for record in records] != canonical:
        raise SystemExit("authoritative generation is not in canonical order")

    index = {record["name"]: record for record in records}

    applied: list[dict[str, object]] = []

    rejected: list[dict[str, object]] = []

    for decision in decisions:
        key = (str(decision["fund"]), str(decision["field"]))

        candidate = by_key.get(key)

        if candidate is None:
            raise SystemExit(f"audit decision has no candidate: {key}")

        verdict = str(decision["decision"])

        if verdict not in APPLIED:
            rejected.append({**decision, "candidate_kind": candidate["kind"]})

            continue

        record = index[key[0]]

        before = record.get(key[1]) or {"status": "MISSING", "value": None}

        record[key[1]] = {
            "status": verdict,
            "value": candidate["new_value"] if verdict != "CONFLICTING" else None,
            "source_url": candidate["new_source_url"],
            "evidence": {
                "document_type": candidate["new_document_type"],
                "page": candidate["new_page"],
                "quote": candidate["new_quote"],
                "scope": candidate["new_scope"],
            },
            "provenance": {
                "generation": arguments.generation,
                "candidate_kind": candidate["kind"],
                "contributing_fixes": candidate.get("contributing_fixes", []),
                "audit_rationale": decision.get("rationale"),
            },
        }

        applied.append(
            {
                "fund": key[0],
                "field": key[1],
                "decision": verdict,
                "kind": candidate["kind"],
                "before_status": before.get("status"),
                "contributing_fixes": candidate.get("contributing_fixes", []),
            }
        )

    enriched_path = (
        REPOSITORY_ROOT / f"data/output/funds.delivery.{arguments.generation}-enriched.json"
    )

    verified_path = (
        REPOSITORY_ROOT / f"data/output/funds.delivery.{arguments.generation}-verified.json"
    )

    if enriched_path.exists() or verified_path.exists():
        raise SystemExit("refusing to overwrite an existing generation")

    enriched_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # The delivery-shaped file keeps only what a reader outside the project may
    # act on: a settled value and the address it came from.
    verified = [
        {
            "name": record["name"],
            "web": record.get("web"),
            **{
                field: (
                    {
                        "status": "found",
                        "value": (record.get(field) or {}).get("value"),
                        "source_url": (record.get(field) or {}).get("source_url"),
                    }
                    if (record.get(field) or {}).get("status") in USABLE
                    and (record.get(field) or {}).get("value") is not None
                    else {"status": "not_found", "value": None}
                )
                for field in DELIVERY_FIELDS
            },
        }
        for record in records
    ]

    verified_path.write_text(
        json.dumps(verified, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = {
        "generation": arguments.generation,
        "enriched": str(enriched_path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "verified": str(verified_path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "decisions": len(decisions),
        "applied": len(applied),
        "rejected": len(rejected),
        "applied_by_decision": dict(Counter(str(item["decision"]) for item in applied)),
        "applied_by_field": dict(Counter(str(item["field"]) for item in applied)),
        "applied_by_kind": dict(Counter(str(item["kind"]) for item in applied)),
        "applied_items": applied,
        "rejected_items": rejected,
    }

    (REPOSITORY_ROOT / f"reports/{arguments.generation}-applied.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({k: v for k, v in summary.items() if not k.endswith("_items")}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
