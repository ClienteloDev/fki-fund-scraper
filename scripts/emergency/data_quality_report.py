"""
Render the unresolved audit flags into reports/INTERIM_DATA_QUALITY_ISSUES.md.

Every flag the stages raised is carried into the consolidated output untouched
and rendered here beside **the value that currently stands** for that fund and
field. That pairing is the point: a flag is not a correction, it is a record
that a delivered value has evidence against it and needs a human decision. The
28 flags fall into four recurring classes - identity/rename, role churn,
liquidation/suspension and document defect.

The report ends with the two review queues that are not flags:
``PROVISIONAL_MEDIUM``, the weakest accepted tier, and the ``SCOPED`` values,
where a scope re-reading can promote a value to fund level without any new
acquisition.

Reads ``reports/emergency-consolidation/consolidation-summary.json`` and the
current enriched output; writes the report. Regenerating it is safe - it is
derived entirely from those two files.

TEMPORARY RECOVERY TOOL.

Usage::

    uv run python scripts/emergency/data_quality_report.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

WORK = REPOSITORY_ROOT / "reports/emergency-consolidation"

ENRICHED = REPOSITORY_ROOT / "data/output/funds.delivery.current-enriched.json"

REPORT = REPOSITORY_ROOT / "reports/INTERIM_DATA_QUALITY_ISSUES.md"

FIELDS = [
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
]


def render_value(records, fund, fieldspec):
    """Render the value that currently stands for a flagged fund and field."""

    record = records.get(fund)

    if record is None:
        return "—"

    names = [name.strip() for name in (fieldspec or "").split(",") if name.strip() in FIELDS]

    if not names:
        return "_(no single field — identity / document-level flag)_"

    parts = []

    for field in names:
        slot = record.get(field) or {}

        value = slot.get("value")

        if isinstance(value, dict):
            value = value.get("name") or value.get("text") or json.dumps(value, ensure_ascii=False)

        rendered = str(value) if value is not None else "—"

        if len(rendered) > 180:
            rendered = rendered[:177] + "…"

        parts.append(f"`{field}` = **{slot.get('status', 'MISSING')}** — {rendered}")

    return "<br>".join(parts)


def escape(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def main():
    log = json.loads((WORK / "consolidation-summary.json").read_text(encoding="utf-8"))

    records = {
        record["name"]: record for record in json.loads(ENRICHED.read_text(encoding="utf-8"))
    }

    rows = []

    for flag in log["flag_log"]:
        fieldspec = flag.get("field") or ""

        rows.append(
            {
                "stage": flag.get("stage"),
                "fund": flag["fund"],
                "field": fieldspec or "(record-level)",
                "issue": (
                    flag.get("issue")
                    or (f"{fieldspec} — review" if fieldspec else "identity / scope")
                ),
                "current_value": render_value(records, flag["fund"], fieldspec),
                "evidence": flag.get("detail") or flag.get("note") or flag.get("flag") or "",
                "source": flag.get("source") or "—",
                "action": flag.get("action") or "recorded, nothing changed",
                "status": flag.get("status", "UNRESOLVED"),
            }
        )

    lines = [
        "# Interim data-quality issues — unresolved audit flags",
        "",
        "Collected from **every** emergency stage delta (stages 01a, 01b, 02 … 17) at "
        "consolidation time. **Nothing here has been fixed.** Every flagged value is still the "
        "value that stands in `data/output/funds.delivery.current-enriched.json`; the flag records "
        "why it needs a human decision.",
        "",
        f"| unresolved flags | {len(rows)} |",
        "|---|---|",
        f"| distinct funds | {len({row['fund'] for row in rows})} |",
        "| status of every flag | `UNRESOLVED` |",
        "| source of truth | `scoped_values` / `audit_flags` arrays inside each record of "
        "`funds.delivery.current-enriched.json` |",
        "",
        "## Recurring classes",
        "",
        "1. **Identity / rename** — the canonical `data/input/funds.json` name no longer matches "
        "the public register. Nothing was renamed; values stay under the canonical name.",
        "2. **Role churn** — manager and/or administrator changed after the document the "
        "delivered value was extracted from. Delivered values were **not** overwritten.",
        "3. **Liquidation / suspension** — the fund is `v likvidaci`, has suspended issuance or "
        "redemptions, or has bought back all shares. Values are historic.",
        "4. **Document defect** — image-only scan, ESEF 0-page text layer, template contamination "
        "or an internally inconsistent report.",
        "",
        "## Flags",
        "",
    ]

    for index, row in enumerate(rows, 1):
        lines += [
            f"### {index}. {row['fund']} — {row['issue']}",
            "",
            f"- **stage** {row['stage']} · **field** `{row['field']}` · "
            f"**status** `{row['status']}`",
            f"- **current value** — {row['current_value']}",
            f"- **conflicting / suspicious evidence** — {escape(row['evidence'])}",
            f"- **source** — {escape(row['source'])}",
            f"- **action taken** — {escape(row['action'])}",
            "",
        ]

    medium = [
        (name, field)
        for name, record in records.items()
        for field in FIELDS
        if (record.get(field) or {}).get("status") == "PROVISIONAL_MEDIUM"
    ]

    lines += [
        "## Secondary review queue (not flags)",
        "",
        f"`PROVISIONAL_MEDIUM` is the weakest accepted tier: **{len(medium)} slots** across "
        f"**{len({name for name, _ in medium})} funds**. They are not audit flags — they were "
        "accepted under the tier rules — but they are the first place to look in a quality pass.",
        "",
        "| field | PROVISIONAL_MEDIUM slots |",
        "|---|---|",
    ]

    for field, count in Counter(field for _, field in medium).most_common():
        lines.append(f"| `{field}` | {count} |")

    lines += [
        "",
        f"`SCOPED` values preserved but **not** counted toward the fund-level KPI: "
        f"**{len(log['scoped_log'])}**. They live in each record's `scoped_values` array with "
        "their own source, evidence and `not_counted_reason`, and are the second review queue: a "
        "scope re-reading (e.g. a subfund confirmed as the fund's only subfund) can promote one "
        "to fund level without any new acquisition.",
        "",
    ]

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("flags:", len(rows), "prov_medium:", len(medium), "scoped:", len(log["scoped_log"]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    main()
