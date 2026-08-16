"""
Rebuild the current database from the baseline and the 18 stage deltas.

This is the reconstruction of ``funds.delivery.current-enriched.json`` and its
delivery projection ``…current-verified.json``. It reads the authoritative
baseline and every delta, applies them in chronological order, and asserts
while doing so that:

- every delta on disk is in the applied list and vice versa,
- the baseline is byte-identical to its recorded sha256, before and after,
- the record count is 341 and the canonical order is preserved,
- **a slot already populated in the baseline is never overwritten** - a value
  arriving for such a slot is preserved as a ``corroborating_values`` entry
  instead,
- the same (fund, field) is never applied twice,
- a value whose ``scope_class`` is outside {FUND, FUND_UNIFORM, SOLE_SUBFUND}
  is preserved in ``scoped_values`` and counted nowhere,
- every applied value carries a tier, a source and evidence.

``norm`` is the only place that reconciles the three applied-value schema
generations the emergency work accumulated: stages 01a-12 use
``tier``/``scope``/``source_url`` with a structured ``value`` object, stage 13
uses ``tier``/``scope_class``/``source`` with a string ``value``, and stages
14-17 use ``confidence``/``scope_class``/``source`` with a string ``value``.
Emergency values therefore carry a **string** ``value`` where the baseline
carries an object; any consumer that introspects ``value`` must handle both.

TEMPORARY RECOVERY TOOL, but the one that has to survive: without it the
current database cannot be rebuilt from its inputs.

**Safety.** Rerunning this on unchanged inputs rewrites byte-identical files.
If what it would write differs from what is on disk, it refuses and says so;
pass ``--force`` only when the difference is intended and the current outputs
have been backed up.

Usage::

    uv run python scripts/emergency/consolidate.py [--force]
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

BASELINE = REPOSITORY_ROOT / "data/output/funds.delivery.offline-recovery1-enriched.json"

BASELINE_SHA = "6304c1cf203fdd08c5aad6e4bd7bcc746cdf5e3b47895dfde13246ec0b129242"

CANONICAL = REPOSITORY_ROOT / "data/input/funds.json"

EMERGENCY = REPOSITORY_ROOT / "data/output/emergency"

OUT_ENRICHED = REPOSITORY_ROOT / "data/output/funds.delivery.current-enriched.json"

OUT_VERIFIED = REPOSITORY_ROOT / "data/output/funds.delivery.current-verified.json"

# Derived accounting, in the gitignored reports tree rather than next to the code.
WORK = REPOSITORY_ROOT / "reports/emergency-consolidation"

DELTAS = [
    "worker-1-sample-delta.json",  # stage 1 part 1
    "stage-01-part2-delta.json",  # stage 1 part 2
    *[f"stage-{index:02d}-delta.json" for index in range(2, 18)],
]

STAGE_LABEL = {
    "worker-1-sample-delta.json": "01a",
    "stage-01-part2-delta.json": "01b",
    **{f"stage-{index:02d}-delta.json": f"{index:02d}" for index in range(2, 18)},
}

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

COUNTED_SCOPE = {"FUND", "FUND_UNIFORM", "SOLE_SUBFUND"}

TIERS = ("VERIFIED", "PROVISIONAL_HIGH", "PROVISIONAL_MEDIUM")

CARRIED = (
    "as_of",
    "scope_class",
    "scope",
    "condition",
    "identity_evidence",
    "rationale",
    "hub_url",
    "corroborating_url",
    "currency",
    "page",
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def norm(value):
    """Normalise the three applied_value schema generations into one shape."""

    return {
        "confidence": value.get("tier") or value.get("confidence"),
        "source_url": value.get("source_url") or value.get("source"),
        "scope_class": value.get("scope_class"),  # absent in stages 01a/01b/02
        "scope": value.get("scope"),  # free-text scope note
        "value": value.get("value"),  # dict (stages 01a-12) or str (stages 13-17)
        "evidence": value.get("evidence"),
        "as_of": value.get("as_of"),
        "condition": value.get("condition"),
        "identity_evidence": value.get("identity_evidence"),
        "rationale": value.get("rationale"),
        "hub_url": value.get("hub_url"),
        "corroborating_url": value.get("corroborating_url"),
        "currency": value.get("currency"),
        "page": value.get("page"),
    }


def status_of(slot):
    return (slot or {}).get("status", "MISSING")


def write_guarded(path, payload, force):
    """Write, unless that would silently change a file already on disk."""

    if path.exists():
        if path.read_text(encoding="utf-8") == payload:
            print(f"{path.name}: reconstruction is identical, left untouched")

            return

        if not force:
            raise SystemExit(
                f"refusing to overwrite {path.name}: the reconstruction differs from the file "
                f"on disk. Back the current outputs up and rerun with --force if that is intended."
            )

    path.write_text(payload, encoding="utf-8")


def main(force=False):
    on_disk = sorted(path.name for path in EMERGENCY.glob("*-delta.json"))

    missing = [name for name in DELTAS if name not in on_disk]

    extra = [name for name in on_disk if name not in DELTAS]

    assert not missing, f"delta files listed but absent from disk: {missing}"

    assert not extra, f"delta files on disk but NOT applied: {extra}"

    assert len(set(DELTAS)) == len(DELTAS), "duplicate stage in DELTAS"

    assert sha256(BASELINE) == BASELINE_SHA, "baseline modified!"

    records = json.loads(BASELINE.read_text(encoding="utf-8"))

    canonical = json.loads(CANONICAL.read_text(encoding="utf-8"))

    if isinstance(canonical, dict):
        canonical = canonical.get("funds", canonical)

    canonical_names = [fund["name"] if isinstance(fund, dict) else fund for fund in canonical]

    assert len(records) == len(canonical_names) == 341

    assert [record["name"] for record in records] == canonical_names, (
        "canonical order differs from baseline"
    )

    baseline_slot = {
        record["name"]: {field: copy.deepcopy(record.get(field)) for field in FIELDS}
        for record in records
    }

    out = copy.deepcopy(records)

    by_name = {record["name"]: record for record in out}

    applied_log, refused, scoped_log, flag_log = [], [], [], []

    processed_funds, filled_by = set(), {}

    for delta_file in DELTAS:
        delta = json.loads((EMERGENCY / delta_file).read_text(encoding="utf-8"))

        stage = STAGE_LABEL[delta_file]

        provenance = {
            "origin": "emergency_enrichment",
            "stage": stage,
            "delta_file": f"data/output/emergency/{delta_file}",
        }

        for entry in delta.get("per_fund", []):
            processed_funds.add(entry["fund"])

        for value in delta.get("applied_values", []):
            fund, field = value["fund"], value["field"]

            record = by_name.get(fund)

            assert record is not None, f"{delta_file}: unknown fund {fund}"

            assert field in FIELDS, f"{delta_file}: unknown field {field}"

            processed_funds.add(fund)

            normalized = norm(value)

            entry = {"stage": stage, "fund": fund, "field": field, **normalized}

            scope_class = normalized["scope_class"]

            if scope_class is not None and scope_class not in COUNTED_SCOPE:
                entry["not_counted_reason"] = f"scope_class {scope_class} is not fund-level"

                record.setdefault("scoped_values", []).append({**entry, "provenance": provenance})

                scoped_log.append(entry)

                continue

            populated = status_of(baseline_slot[fund][field]) != "MISSING"

            if populated or (fund, field) in filled_by:
                entry["not_applied_reason"] = (
                    "slot already populated in the delivery baseline"
                    if populated
                    else f"already supplied by stage {filled_by[(fund, field)]}"
                )

                record.setdefault("corroborating_values", []).append(
                    {**entry, "provenance": provenance}
                )

                refused.append(entry)

                continue

            assert normalized["confidence"] in TIERS, (
                f"{delta_file}: bad tier {normalized['confidence']!r} {fund}/{field}"
            )

            assert normalized["source_url"], f"{delta_file}: no source for {fund}/{field}"

            assert normalized["evidence"], f"{delta_file}: no evidence for {fund}/{field}"

            slot = {
                "status": normalized["confidence"],
                "value": normalized["value"],
                "source_url": normalized["source_url"],
                "evidence": normalized["evidence"],
            }

            for name in CARRIED:
                if normalized.get(name) is not None:
                    slot[name] = normalized[name]

            slot["provenance"] = provenance

            record[field] = slot

            filled_by[(fund, field)] = stage

            applied_log.append(entry)

        for scoped in delta.get("scoped_not_counted", []):
            record = by_name.get(scoped["fund"])

            assert record is not None, f"{delta_file}: unknown fund {scoped['fund']}"

            entry = {key: value for key, value in scoped.items() if key != "kind"}

            entry.update(
                stage=stage,
                provenance=provenance,
                not_counted_reason="SCOPED (subfund / share class) - preserved, not counted",
            )

            record.setdefault("scoped_values", []).append(entry)

            scoped_log.append(entry)

        for flag in delta.get("audit_flags", []):
            record = by_name.get(flag["fund"])

            assert record is not None, f"{delta_file}: unknown fund {flag['fund']}"

            entry = {key: value for key, value in flag.items() if key != "kind"}

            entry.update(stage=stage, provenance=provenance, status="UNRESOLVED")

            record.setdefault("audit_flags", []).append(entry)

            flag_log.append(entry)

        assert not delta.get("audit_failures"), f"{delta_file} has audit_failures"

    assert len(out) == 341

    assert [record["name"] for record in out] == canonical_names, "canonical order broken"

    for record in out:
        for field in FIELDS:
            before, now = baseline_slot[record["name"]][field], record.get(field)

            if status_of(before) != "MISSING":
                assert now == before, f"OVERWRITE/DELETION {record['name']}/{field}"
            elif status_of(now) != "MISSING":
                assert now["provenance"]["origin"] == "emergency_enrichment"

                assert now["status"] in TIERS and now["source_url"] and now["evidence"]

    write_guarded(OUT_ENRICHED, json.dumps(out, ensure_ascii=False, indent=1), force)

    verified = []

    for record in out:
        projection = {"name": record["name"], "web": record.get("web")}

        for field in FIELDS:
            slot = record.get(field) or {}

            if status_of(slot) == "MISSING":
                projection[field] = {"status": "not_found", "value": None, "source_url": None}
            else:
                projection[field] = {
                    "status": "found",
                    "value": slot.get("value"),
                    "source_url": slot.get("source_url"),
                }

        verified.append(projection)

    write_guarded(OUT_VERIFIED, json.dumps(verified, ensure_ascii=False, indent=1), force)

    assert sha256(BASELINE) == BASELINE_SHA, "baseline modified during the run!"

    baseline_usable = sum(
        1 for record in records for field in FIELDS if status_of(record.get(field)) != "MISSING"
    )

    usable = sum(
        1 for record in out for field in FIELDS if status_of(record.get(field)) != "MISSING"
    )

    summary = {
        "deltas_applied": DELTAS,
        "delta_files_on_disk": on_disk,
        "records": len(out),
        "processed_funds": len(processed_funds),
        "baseline_usable": baseline_usable,
        "usable": usable,
        "coverage_pct": round(usable / 3751 * 100, 2),
        "applied": len(applied_log),
        "refused_overwrite_or_duplicate": len(refused),
        "scoped_not_counted": len(scoped_log),
        "audit_flags": len(flag_log),
        "sha256_enriched": sha256(OUT_ENRICHED),
        "sha256_verified": sha256(OUT_VERIFIED),
    }

    WORK.mkdir(parents=True, exist_ok=True)

    (WORK / "consolidation-summary.json").write_text(
        json.dumps(
            {
                **summary,
                "applied_log": applied_log,
                "refused": refused,
                "scoped_log": scoped_log,
                "flag_log": flag_log,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    main(force="--force" in sys.argv)
