"""
Phases 12 to 14 of the 8-fund crawler recovery experiment.

Compares what the deterministic pipeline delivers for the sample before
and after the fixes, writes the two isolated sample outputs, and reports
the coverage change per fund and per field.

Both generations are the same run of the same crawler over the same eight
canonical start pages. Only the code between them differs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sample8_baseline import DELIVERY_FIELDS, SAMPLE_FUND_NAMES  # noqa: E402

from fundscraper.delivery_export import build_delivery_records  # noqa: E402
from fundscraper.output_audit import (  # noqa: E402
    AuditStatus,
    audit_enriched_output,
    file_digest,
    load_enriched_records,
)

SAMPLE_ROOT = REPOSITORY_ROOT / "cache/sample8-crawler-recovery"

# What the audit says about a field it did not doubt. A field the audit
# reported is delivered only as provisional, never as verified.
BLOCKING = frozenset(
    {
        AuditStatus.SUSPICIOUS.value,
        AuditStatus.CONFLICTING.value,
        AuditStatus.REJECTED.value,
    }
)


def sample_records(generation: str) -> list[dict[str, object]]:
    path = SAMPLE_ROOT / generation / "funds.enriched.json"

    records = json.loads(path.read_text(encoding="utf-8"))

    wanted = set(SAMPLE_FUND_NAMES)

    return [record for record in records if record.get("name") in wanted]


def audit_findings(generation: str) -> list[dict[str, object]]:
    """Audit only the sample, from a file holding only the sample."""

    sample_path = SAMPLE_ROOT / generation / "sample-only.enriched.json"

    sample_path.write_text(
        json.dumps(sample_records(generation), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = audit_enriched_output(
        records=load_enriched_records(sample_path),
        input_path=sample_path,
        input_sha256=file_digest(sample_path),
    )

    return [finding.model_dump(mode="json") for finding in report.findings]


def field_state(record: dict[str, object], blocked: set[tuple[str, str]]) -> dict[str, str]:
    """Return the delivered tier of every field of one fund."""

    name = str(record.get("name") or "")

    state: dict[str, str] = {}

    for field in DELIVERY_FIELDS:
        payload = record.get(field)

        status = payload.get("status") if isinstance(payload, dict) else None

        if status != "found":
            state[field] = "MISSING"

            continue

        state[field] = "PROVISIONAL_HIGH" if (name, field) in blocked else "VERIFIED"

    return state


def blocked_pairs(findings: list[dict[str, object]]) -> set[tuple[str, str]]:
    return {
        (str(finding.get("fund_name") or ""), str(finding.get("field") or ""))
        for finding in findings
        if str(finding.get("status") or "") in BLOCKING
    }


def evidence_of(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None

    source = payload.get("source")

    holder = source.get("source") if isinstance(source, dict) and "source" in source else source

    if not isinstance(holder, dict):
        holder = {}

    return {
        "source_url": holder.get("url"),
        "document_type": holder.get("document_type"),
        "page": (source or {}).get("page_number") if isinstance(source, dict) else None,
        "quote": (source or {}).get("quote") if isinstance(source, dict) else None,
        "scope": payload.get("scope"),
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    generations = ("before", "after")

    states: dict[str, dict[str, dict[str, str]]] = {}

    records_by_generation: dict[str, dict[str, dict[str, object]]] = {}

    for generation in generations:
        findings = audit_findings(generation)

        blocked = blocked_pairs(findings)

        records = {str(record["name"]): record for record in sample_records(generation)}

        records_by_generation[generation] = records

        states[generation] = {
            name: field_state(records[name], blocked) for name in SAMPLE_FUND_NAMES
        }

    baseline = json.loads(
        (REPOSITORY_ROOT / "reports/sample8-before.json").read_text(encoding="utf-8")
    )

    baseline_by_name = {row["name"]: row for row in baseline["funds"]}

    funds_report: list[dict[str, object]] = []

    for name in SAMPLE_FUND_NAMES:
        before = states["before"][name]

        after = states["after"][name]

        usable_before = [field for field, tier in before.items() if tier != "MISSING"]

        usable_after = [field for field, tier in after.items() if tier != "MISSING"]

        funds_report.append(
            {
                "name": name,
                "canonical_input_url": baseline_by_name[name]["input_web"],
                "production_delivery_usable": baseline_by_name[name]["usable_fields"],
                "own_site_deterministic_before": len(usable_before),
                "own_site_deterministic_after": len(usable_after),
                "gained": sorted(set(usable_after) - set(usable_before)),
                "lost": sorted(set(usable_before) - set(usable_after)),
                "still_missing": [field for field in DELIVERY_FIELDS if field not in usable_after],
                "field_tier_before": before,
                "field_tier_after": after,
                "evidence_after": {
                    field: evidence_of(records_by_generation["after"][name].get(field))
                    for field in usable_after
                },
            }
        )

    slots = len(SAMPLE_FUND_NAMES) * len(DELIVERY_FIELDS)

    total_before = sum(int(row["own_site_deterministic_before"]) for row in funds_report)

    total_after = sum(int(row["own_site_deterministic_after"]) for row in funds_report)

    by_field = {
        field: {
            "before": sum(
                1
                for row in funds_report
                if row["field_tier_before"][field] != "MISSING"  # type: ignore[index]
            ),
            "after": sum(
                1
                for row in funds_report
                if row["field_tier_after"][field] != "MISSING"  # type: ignore[index]
            ),
        }
        for field in DELIVERY_FIELDS
    }

    payload = {
        "sample_size": len(SAMPLE_FUND_NAMES),
        "fields_per_fund": len(DELIVERY_FIELDS),
        "theoretical_slots": slots,
        "seeds": "canonical FundInput.web only, both generations",
        "own_site_deterministic_before": total_before,
        "own_site_deterministic_after": total_after,
        "coverage_before": round(total_before / slots, 4),
        "coverage_after": round(total_after / slots, 4),
        "production_delivery_usable": baseline["usable_total"],
        "by_field": by_field,
        "funds": funds_report,
    }

    (REPOSITORY_ROOT / "reports/sample8-crawler-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # The two isolated sample outputs. Neither is merged into the 341-fund
    # delivery; both hold only these eight funds.
    after_records = [records_by_generation["after"][name] for name in SAMPLE_FUND_NAMES]

    verified = build_delivery_records(
        records=after_records,
        audit_findings=audit_findings("after"),
    )

    (REPOSITORY_ROOT / "data/output/sample8.crawler-recovery-verified.json").write_text(
        json.dumps(verified, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    enriched = [
        {
            "name": name,
            "web": baseline_by_name[name]["input_web"],
            **{
                field: {
                    "status": states["after"][name][field],
                    "value": (
                        records_by_generation["after"][name].get(field, {}).get("value")  # type: ignore[union-attr]
                        if states["after"][name][field] != "MISSING"
                        else None
                    ),
                    "evidence": (
                        evidence_of(records_by_generation["after"][name].get(field))
                        if states["after"][name][field] != "MISSING"
                        else None
                    ),
                }
                for field in DELIVERY_FIELDS
            },
        }
        for name in SAMPLE_FUND_NAMES
    ]

    (REPOSITORY_ROOT / "data/output/sample8.crawler-recovery-enriched.json").write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Sample 8 — deterministic pipeline, own site only",
        "",
        f"Slots {slots} · before **{total_before}** ({total_before / slots:.1%}) "
        f"→ after **{total_after}** ({total_after / slots:.1%})",
        "",
        "Both runs start from the canonical `web` value and from nothing else.",
        "",
        "| fund | production delivery | own site before | own site after | gained |",
        "|---|---:|---:|---:|---|",
    ]

    for row in funds_report:
        gained = row["gained"]

        assert isinstance(gained, list)

        lines.append(
            f"| {row['name']} | {row['production_delivery_usable']}/11 "
            f"| {row['own_site_deterministic_before']}/11 "
            f"| {row['own_site_deterministic_after']}/11 | {', '.join(gained) or '—'} |"
        )

    lines.extend(["", "## By field", "", "| field | before | after |", "|---|---:|---:|"])

    for field, counts in by_field.items():
        lines.append(f"| {field} | {counts['before']}/8 | {counts['after']}/8 |")

    # The generated tables only. The written report at
    # reports/sample8-crawler-report.md carries them together with the
    # analysis, and re-running this script must not clobber it.
    (REPOSITORY_ROOT / "reports/sample8-crawler-tables.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print("\n".join(lines))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
