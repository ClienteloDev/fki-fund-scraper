"""
Phase 9 of the 8-fund crawler recovery experiment: where each value is lost.

For every fund and field the sample did not deliver, this names the
earliest stage at which the value disappeared. The stage is read from what
the run itself recorded - the acquisition manifest, the discovery log and
the reason the extraction stored next to the missing field - not guessed.

Read-only. It consumes the reports the earlier phases wrote.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sample8_baseline import DELIVERY_FIELDS, SAMPLE_FUND_NAMES  # noqa: E402

SAMPLE_ROOT = REPOSITORY_ROOT / "cache/sample8-crawler-recovery"


# The ten stages of the funnel, in the order a value passes through them.
STAGES = (
    "1_root_not_fetched",
    "2_link_not_discovered",
    "3_link_discovered_not_followed",
    "4_page_fetched_not_retained",
    "5_source_attribution_failed",
    "6_html_parser_lost_content",
    "7_pdf_parser_lost_content",
    "8_extractor_produced_no_candidate",
    "9_candidate_ambiguous_or_conflicting",
    "10_semantic_audit_rejected",
)


# What each stored reason means about where the value was lost. A reason
# that names the entity is an attribution loss however the field reports
# it; a reason that says nothing was quantified means the sources were in
# hand and the patterns did not fire.
REASON_STAGE = {
    "entity_not_matched": "5_source_attribution_failed",
    "scope_mismatch": "5_source_attribution_failed",
    "not_quantified": "8_extractor_produced_no_candidate",
    "no_source": "4_page_fetched_not_retained",
    "conflicting_values": "9_candidate_ambiguous_or_conflicting",
    "unsupported_value": "10_semantic_audit_rejected",
}


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def stage_of(
    *,
    status: str,
    reason_code: str,
    sources_acquired: int,
) -> str:
    if sources_acquired == 0:
        return "4_page_fetched_not_retained"

    if status == "conflicting":
        return "9_candidate_ambiguous_or_conflicting"

    mapped = REASON_STAGE.get(reason_code)

    if mapped is not None:
        return mapped

    if status == "ambiguous":
        return "9_candidate_ambiguous_or_conflicting"

    return "8_extractor_produced_no_candidate"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    generation = sys.argv[1] if len(sys.argv) > 1 else "before"

    records = {
        str(record["name"]): record
        for record in load(SAMPLE_ROOT / generation / "funds.enriched.json")  # type: ignore[union-attr]
    }

    manifest = load(REPOSITORY_ROOT / f"reports/sample8-acquisition-manifest-{generation}.json")

    acquired = {
        str(row["fund_name"]): row
        for row in manifest["funds"]  # type: ignore[index,call-overload]
    }

    rows: list[dict[str, object]] = []

    counts: Counter[str] = Counter()

    for name in SAMPLE_FUND_NAMES:
        record = records[name]

        site = acquired[name]

        for field in DELIVERY_FIELDS:
            payload = record.get(field) or {}

            status = str(payload.get("status") or "pending")

            if status == "found":
                continue

            reason = payload.get("reason") or {}

            reason_code = str(reason.get("code") or "")

            stage = stage_of(
                status=status,
                reason_code=reason_code,
                sources_acquired=int(site["sources_acquired"]),
            )

            counts[stage] += 1

            rows.append(
                {
                    "fund_name": name,
                    "field": field,
                    "status": status,
                    "reason_code": reason_code,
                    "reason_detail": reason.get("detail"),
                    "stage": stage,
                    "sources_acquired": site["sources_acquired"],
                    "html_acquired": site["html_acquired"],
                    "documents_acquired": site["documents_acquired"],
                    "relevant_pages_refused": site["relevant_pages_refused_total"],
                    # What the extraction actually looked at, when it
                    # recorded it. This is what tells an attribution loss
                    # from a source that was never there.
                    "attempted_sources": [
                        {
                            "url": item.get("url"),
                            "outcome": item.get("outcome"),
                            "document_type": item.get("document_type"),
                            "detail": (str(item.get("detail") or ""))[:400],
                        }
                        for item in (payload.get("attempted_sources") or [])[:4]
                    ],
                }
            )

    by_fund = Counter(str(row["fund_name"]) for row in rows)

    by_field = Counter(str(row["field"]) for row in rows)

    payload = {
        "generation": generation,
        "sample_size": len(SAMPLE_FUND_NAMES),
        "theoretical_slots": len(SAMPLE_FUND_NAMES) * len(DELIVERY_FIELDS),
        "delivered": len(SAMPLE_FUND_NAMES) * len(DELIVERY_FIELDS) - len(rows),
        "lost": len(rows),
        "by_stage": {stage: counts.get(stage, 0) for stage in STAGES},
        "by_fund": dict(by_fund.most_common()),
        "by_field": dict(by_field.most_common()),
        "losses": rows,
    }

    (REPOSITORY_ROOT / f"reports/sample8-loss-analysis-{generation}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        f"# Sample 8 — where each value is lost ({generation})",
        "",
        f"Slots {payload['theoretical_slots']} · delivered {payload['delivered']} · "
        f"lost {payload['lost']}",
        "",
        "Every fund started from its canonical `web` value alone.",
        "",
        "| stage | lost values |",
        "|---|---:|",
    ]

    for stage in STAGES:
        lines.append(f"| {stage} | {counts.get(stage, 0)} |")

    lines.extend(["", "## By field", "", "| field | lost of 8 |", "|---|---:|"])

    for field in DELIVERY_FIELDS:
        lines.append(f"| {field} | {by_field.get(field, 0)} |")

    lines.extend(["", "## By fund", "", "| fund | lost of 11 |", "|---|---:|"])

    for name in SAMPLE_FUND_NAMES:
        lines.append(f"| {name} | {by_fund.get(name, 0)} |")

    (REPOSITORY_ROOT / f"reports/sample8-loss-analysis-{generation}.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print("\n".join(lines))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
