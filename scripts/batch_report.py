"""
Compare what a validation batch delivered before and after the fixes.

Reads the two generations the crawl produced, the loss funnels, the fix
attribution and the acquisition manifests, and writes the batch report together
with the batch's own isolated delivery outputs. Nothing is merged into the
341-fund delivery.

Usage::

    uv run python scripts/batch_report.py <batch>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]

sys.path.insert(0, str(SCRIPT_DIRECTORY))

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_selection import DELIVERY_FIELDS, resolve  # noqa: E402

from fundscraper.delivery_export import build_delivery_records  # noqa: E402
from fundscraper.output_audit import (  # noqa: E402
    AuditStatus,
    audit_enriched_output,
    file_digest,
    load_enriched_records,
)

BLOCKING = frozenset(
    {
        AuditStatus.SUSPICIOUS.value,
        AuditStatus.CONFLICTING.value,
        AuditStatus.REJECTED.value,
    }
)


def read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_findings(*, batch_root: Path, generation: str, names: tuple[str, ...]) -> list[dict]:
    """Audit only the batch, from a file holding only the batch."""

    source = batch_root / generation / "funds.enriched.json"

    wanted = set(names)

    records = [item for item in read(source) if item.get("name") in wanted]  # type: ignore[union-attr]

    path = batch_root / generation / "batch-only.enriched.json"

    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report = audit_enriched_output(
        records=load_enriched_records(path),
        input_path=path,
        input_sha256=file_digest(path),
    )

    return [finding.model_dump(mode="json") for finding in report.findings]


def tiers(
    *,
    record: dict,
    blocked: set[tuple[str, str]],
) -> dict[str, str]:
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

    batch_name = sys.argv[1] if len(sys.argv) > 1 else "batch10"

    batch = resolve(batch_name)

    names = batch.fund_names

    states: dict[str, dict[str, dict[str, str]]] = {}

    records_by_generation: dict[str, dict[str, dict]] = {}

    for generation in ("before", "after"):
        findings = audit_findings(
            batch_root=batch.root,
            generation=generation,
            names=names,
        )

        blocked = {
            (str(item.get("fund_name") or ""), str(item.get("field") or ""))
            for item in findings
            if str(item.get("status") or "") in BLOCKING
        }

        records = {
            str(item["name"]): item
            for item in read(batch.root / generation / "funds.enriched.json")  # type: ignore[union-attr]
            if item.get("name") in set(names)
        }

        records_by_generation[generation] = records

        states[generation] = {name: tiers(record=records[name], blocked=blocked) for name in names}

    losses = {
        generation: read(REPOSITORY_ROOT / f"reports/{batch.name}-loss-analysis-{generation}.json")
        for generation in ("before", "after")
    }

    manifests = {
        generation: read(
            REPOSITORY_ROOT / f"reports/{batch.name}-acquisition-manifest-{generation}.json"
        )
        for generation in ("before", "after")
    }

    attribution_path = REPOSITORY_ROOT / f"reports/{batch.name}-fix-attribution-after.json"

    attribution = read(attribution_path) if attribution_path.exists() else {"funds": []}

    attribution_by_fund = {
        str(row["fund_name"]): row
        for row in attribution.get("funds", [])  # type: ignore[union-attr]
    }

    production = {
        item["name"]: item
        for item in read(REPOSITORY_ROOT / "data/output/funds.delivery.autonomous4-enriched.json")  # type: ignore[union-attr]
    }

    usable_statuses = {"VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM"}

    acquired = {
        generation: {str(row["fund_name"]): row for row in payload["funds"]}  # type: ignore[index]
        for generation, payload in manifests.items()
    }

    funds_report: list[dict[str, object]] = []

    gain_by_field: Counter[str] = Counter()

    for name in names:
        before = states["before"][name]

        after = states["after"][name]

        usable_before = [field for field, tier in before.items() if tier != "MISSING"]

        usable_after = [field for field, tier in after.items() if tier != "MISSING"]

        gained = sorted(set(usable_after) - set(usable_before))

        for field in gained:
            gain_by_field[field] += 1

        production_record = production.get(name, {})

        funds_report.append(
            {
                "name": name,
                "role": batch.role_of(name),
                "production_delivery_usable": sum(
                    1
                    for field in DELIVERY_FIELDS
                    if (production_record.get(field) or {}).get("status") in usable_statuses
                ),
                "own_site_before": len(usable_before),
                "own_site_after": len(usable_after),
                "gained": gained,
                "lost": sorted(set(usable_before) - set(usable_after)),
                "still_missing": [f for f in DELIVERY_FIELDS if f not in usable_after],
                "sources_acquired_before": acquired["before"][name]["sources_acquired"],
                "sources_acquired_after": acquired["after"][name]["sources_acquired"],
                "field_tier_before": before,
                "field_tier_after": after,
                "fix_attribution": (attribution_by_fund.get(name, {}) or {}).get("attribution", {}),
                "evidence_after": {
                    field: evidence_of(records_by_generation["after"][name].get(field))
                    for field in usable_after
                },
            }
        )

    slots = len(names) * len(DELIVERY_FIELDS)

    total_before = sum(int(row["own_site_before"]) for row in funds_report)

    total_after = sum(int(row["own_site_after"]) for row in funds_report)

    payload = {
        "batch": batch.name,
        "sample_size": len(names),
        "fields_per_fund": len(DELIVERY_FIELDS),
        "theoretical_slots": slots,
        "seeds": "canonical FundInput.web only, both generations",
        "own_site_before": total_before,
        "own_site_after": total_after,
        "coverage_before": round(total_before / slots, 4),
        "coverage_after": round(total_after / slots, 4),
        "gain_by_field": dict(gain_by_field.most_common()),
        "credit_by_fix": attribution.get("credit_by_fix", {}),  # type: ignore[union-attr]
        "loss_by_stage": {
            generation: payload_["by_stage"]  # type: ignore[index]
            for generation, payload_ in losses.items()
        },
        "by_role": {
            role: {
                "before": sum(
                    int(row["own_site_before"]) for row in funds_report if row["role"] == role
                ),
                "after": sum(
                    int(row["own_site_after"]) for row in funds_report if row["role"] == role
                ),
                "funds": [row["name"] for row in funds_report if row["role"] == role],
            }
            for role in sorted({str(row["role"]) for row in funds_report})
        },
        "funds": funds_report,
    }

    (REPOSITORY_ROOT / f"reports/{batch.name}-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    after_records = [records_by_generation["after"][name] for name in names]

    verified = build_delivery_records(
        records=after_records,
        audit_findings=audit_findings(
            batch_root=batch.root,
            generation="after",
            names=names,
        ),
    )

    (REPOSITORY_ROOT / f"data/output/{batch.name}.crawler-validation-verified.json").write_text(
        json.dumps(verified, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    enriched = [
        {
            "name": name,
            "web": records_by_generation["after"][name].get("web"),
            **{
                field: {
                    "status": states["after"][name][field],
                    "value": (
                        (records_by_generation["after"][name].get(field) or {}).get("value")
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
        for name in names
    ]

    (REPOSITORY_ROOT / f"data/output/{batch.name}.crawler-validation-enriched.json").write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        f"# {batch.name} — deterministic pipeline, own site only",
        "",
        f"Slots {slots} · before **{total_before}** ({total_before / slots:.1%}) "
        f"→ after **{total_after}** ({total_after / slots:.1%})",
        "",
        "| fund | role | production | before | after | gained |",
        "|---|---|---:|---:|---:|---|",
    ]

    for row in funds_report:
        gained_fields = row["gained"]

        assert isinstance(gained_fields, list)

        lines.append(
            f"| {row['name']} | {row['role']} | {row['production_delivery_usable']}/11 "
            f"| {row['own_site_before']}/11 | {row['own_site_after']}/11 "
            f"| {', '.join(gained_fields) or '—'} |"
        )

    lines.extend(["", "## Gains by field", "", "| field | funds gaining |", "|---|---:|"])

    for field, count in gain_by_field.most_common():
        lines.append(f"| {field} | {count} |")

    lines.extend(["", "## Credit by fix", "", "| fix | values earned |", "|---|---:|"])

    for fix, count in (payload["credit_by_fix"] or {}).items():  # type: ignore[union-attr]
        lines.append(f"| {fix} | {count} |")

    (REPOSITORY_ROOT / f"reports/{batch.name}-tables.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print("\n".join(lines))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
