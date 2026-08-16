"""
Turn the offline re-extraction into a delta against the authoritative delivery.

Only three things are worth a human's or an oracle's attention: a value for a
slot that is currently empty, a different reading of a slot that is currently
filled, and a slot that is currently a single value but now has competing ones.
Everything else is left alone, which is what keeps the audit small enough to be
done properly.

Each candidate is attributed by ablation - the fund is extracted again with one
change switched off, and the fix is credited only if the value disappears.
Only changes that *could* have affected the field are tried: the boilerplate
token only matters to a fund whose registered name carries the phrase, the scale
word only to a minimum investment, the clause reader only to fees, the table
reader only to a corpus that has tables. Nothing is credited without the run
proving it, and a value that survives every ablation is reported as OTHER.

Usage::

    uv run python scripts/offline_delta.py \\
        --extraction cache/offline-recovery1/extraction.jsonl \\
        --authoritative data/output/funds.delivery.autonomous4-enriched.json \\
        --database cache/regen.sqlite3 \\
        --isin-register cache/isin-recovery/isin-hits.json \\
        --output reports/offline-recovery1-delta.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]

sys.path.insert(0, str(SCRIPT_DIRECTORY))

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_ablation import _disabled, without_markup_tables  # noqa: E402
from batch_selection import DELIVERY_FIELDS  # noqa: E402
from offline_reextract import field_payload, load_isin_register, read_documents  # noqa: E402

from fundscraper.extended_extraction import extract_extended_fields  # noqa: E402
from fundscraper.field_extraction import (  # noqa: E402
    extract_fund_fields,
    official_isin_identity,
)
from fundscraper.html_discovery import normalize_search_text  # noqa: E402
from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.output_service import stable_fund_id  # noqa: E402

USABLE = ("VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM")


# key -> (reported label, the label batch_ablation switches off)
ABLATIONS = {
    "1": ("FIX_1_IDENTITY_BOILERPLATE", "1_identity_boilerplate_token"),
    "3": ("FIX_3_HTML_TABLE", None),
    "4": ("FIX_4_SCALED_MINIMUM", "4_scaled_money_minimum"),
    "5": ("FIX_5_FEE_SCHEDULE", "5_fee_tier_clause"),
    "isin": ("EXISTING_ISIN_IDENTITY", None),
}


def applicable(*, fund_name: str, field: str, markup_tables: int, has_register: bool) -> set[str]:
    """
    Return the changes that could possibly have earned a value of this field.

    This is a statement about what each change touches, not a guess about this
    value: a change that cannot reach the field is not worth an extraction.
    """

    keys: set[str] = set()

    if "zakladnim" in normalize_search_text(fund_name):
        keys.add("1")

    if markup_tables > 0:
        keys.add("3")

    if field == "minimum_investment":
        keys.add("4")

    if field == "fees":
        keys.add("5")

    if has_register:
        keys.add("isin")

    return keys


def value_signature(payload: object) -> str | None:
    if payload is None:
        return None

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def found_fields(
    *,
    fund_name: str,
    fund_web: str | None,
    documents: list,
    register: dict,
    key: str | None,
) -> set[str]:
    """Return the fields found with one change switched off."""

    corpus = without_markup_tables(documents) if key == "3" else documents

    active_register = None if key == "isin" else (register or None)

    switch = ABLATIONS[key][1] if key is not None else None

    with _disabled(switch), official_isin_identity(active_register):
        core = extract_fund_fields(
            fund_name=fund_name,
            documents=corpus,
            fund_web=fund_web,
        )

        extended = extract_extended_fields(
            fund_name=fund_name,
            fund_web=fund_web,
            documents=corpus,
        )

    return {
        field
        for field in DELIVERY_FIELDS
        if field_payload(getattr(core, field, None) or getattr(extended, field))["status"]
        == "found"
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(prog="offline_delta")

    parser.add_argument("--extraction", type=Path, required=True)

    parser.add_argument("--authoritative", type=Path, required=True)

    parser.add_argument("--database", type=Path, required=True)

    parser.add_argument("--isin-register", type=Path, default=None)

    parser.add_argument("--output", type=Path, required=True)

    parser.add_argument(
        "--attribute-from",
        type=Path,
        default=None,
        help=(
            "Audit file naming the candidates worth attributing. Attribution costs an "
            "extraction per change per fund, so it is spent on the candidates that "
            "survived the audit rather than on the ones already refused."
        ),
    )

    arguments = parser.parse_args()

    extracted: dict[str, dict] = {}

    for line in arguments.extraction.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)

            extracted[row["name"]] = row

    authoritative = {
        record["name"]: record
        for record in json.loads(arguments.authoritative.read_text(encoding="utf-8"))
    }

    funds = {fund.name: fund for fund in load_funds(REPOSITORY_ROOT / "data/input/funds.json")}

    register = load_isin_register(arguments.isin_register)

    candidates: list[dict] = []

    for name, row in extracted.items():
        current = authoritative.get(name, {})

        for field in DELIVERY_FIELDS:
            new = row["fields"][field]

            old = current.get(field) or {}

            old_status = old.get("status") or "MISSING"

            new_status = new["status"]

            kind: str | None = None

            if old_status == "MISSING" and new_status == "found":
                kind = "A_new_value_for_missing_slot"
            elif old_status in USABLE and new_status == "found":
                if value_signature(new["value"]) != value_signature(old.get("value")):
                    kind = "B_different_interpretation"
            elif old_status in USABLE and new_status == "conflicting":
                kind = "C_conflict_exposed"

            if kind is None:
                continue

            candidates.append(
                {
                    "fund": name,
                    "field": field,
                    "kind": kind,
                    "authoritative_status": old_status,
                    "authoritative_value": old.get("value"),
                    "authoritative_source_url": old.get("source_url"),
                    "new_status": new_status,
                    "new_value": new["value"],
                    "new_source_url": new["source_url"],
                    "new_document_type": new["document_type"],
                    "new_page": new["page"],
                    "new_quote": new["quote"],
                    "new_scope": new["scope"],
                    "markup_tables": row["markup_tables"],
                    "documents": row["documents"],
                }
            )

    print(
        f"candidates: {len(candidates)} over {len({c['fund'] for c in candidates})} funds",
        flush=True,
    )

    print(Counter(str(c["kind"]) for c in candidates).most_common(), flush=True)

    attribute_only: set[str] | None = None

    if arguments.attribute_from is not None:
        attribute_only = {
            str(item["fund"])
            for item in json.loads(arguments.attribute_from.read_text(encoding="utf-8"))
        }

        print(f"attributing {len(attribute_only)} funds named by the audit", flush=True)

    by_fund: dict[str, list[dict]] = {}

    for candidate in candidates:
        fund_name = str(candidate["fund"])

        if attribute_only is not None and fund_name not in attribute_only:
            candidate["contributing_fixes"] = ["NOT_ATTRIBUTED_REFUSED_CANDIDATE"]

            continue

        by_fund.setdefault(fund_name, []).append(candidate)

    connection = sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True)

    started = perf_counter()

    for index, (name, rows) in enumerate(sorted(by_fund.items()), start=1):
        needed: set[str] = set()

        for candidate in rows:
            needed |= applicable(
                fund_name=name,
                field=str(candidate["field"]),
                markup_tables=int(candidate["markup_tables"]),
                has_register=bool(register),
            )

        if not needed:
            for candidate in rows:
                candidate["contributing_fixes"] = ["OTHER"]

            continue

        fund = funds[name]

        documents, _, _ = read_documents(
            connection=connection,
            fund_id=stable_fund_id(fund),
        )

        without: dict[str, set[str]] = {
            key: found_fields(
                fund_name=name,
                fund_web=fund.web,
                documents=documents,
                register=register,
                key=key,
            )
            for key in sorted(needed)
        }

        for candidate in rows:
            field = str(candidate["field"])

            own = applicable(
                fund_name=name,
                field=field,
                markup_tables=int(candidate["markup_tables"]),
                has_register=bool(register),
            )

            earned = [
                ABLATIONS[key][0] for key in sorted(own) if field not in without.get(key, set())
            ]

            candidate["contributing_fixes"] = earned or ["OTHER"]

        print(
            f"[{index}/{len(by_fund)}] {name[:44]:46} "
            f"candidates={len(rows):2} ablations={len(needed)}",
            flush=True,
        )

    connection.close()

    credit: Counter[str] = Counter()

    for candidate in candidates:
        for label in candidate.get("contributing_fixes", ["OTHER"]):
            credit[str(label)] += 1

    payload = {
        "extraction": str(arguments.extraction).replace("\\", "/"),
        "authoritative": str(arguments.authoritative).replace("\\", "/"),
        "funds_extracted": len(extracted),
        "candidates": len(candidates),
        "by_kind": dict(Counter(str(c["kind"]) for c in candidates).most_common()),
        "by_field": dict(Counter(str(c["field"]) for c in candidates).most_common()),
        "credit_by_fix": dict(credit.most_common()),
        "attribution_seconds": round(perf_counter() - started, 1),
        "items": candidates,
    }

    arguments.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\ncredit by fix: {payload['credit_by_fix']}")

    print(f"written {arguments.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
