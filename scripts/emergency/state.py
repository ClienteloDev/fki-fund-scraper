"""
The effective baseline: the authoritative delivery plus every stage delta so far.

Read-only over ``funds.delivery.offline-recovery1-enriched.json`` and the 18
deltas. Every emergency stage started here, because the question a stage had to
answer was never "what does the baseline hold" but "what is still missing after
everything applied so far".

Two rules are enforced in ``load`` and matter more than the arithmetic:

- a value whose ``scope_class`` is outside {FUND, FUND_UNIFORM, SOLE_SUBFUND}
  does not count toward the fund-level KPI, and
- a slot already populated is never counted twice, whoever supplied it.

``sorted_order`` reproduces the work ordering the stages used - poorest funds
first, canonical order as the tie-break - so an earlier stage's selection stays
reproducible after a later one is added.

TEMPORARY RECOVERY TOOL, kept because it is the cheapest way to ask what a
given generation of deltas adds up to.

Usage::

    uv run python scripts/emergency/state.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

BASELINE = REPOSITORY_ROOT / "data/output/funds.delivery.offline-recovery1-enriched.json"

EMERGENCY = REPOSITORY_ROOT / "data/output/emergency"

DELTAS = [
    "worker-1-sample-delta.json",
    "stage-01-part2-delta.json",
    *[f"stage-{index:02d}-delta.json" for index in range(2, 18)],
]

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


def baseline_sha256():
    return hashlib.sha256(BASELINE.read_bytes()).hexdigest()


def load():
    """Return (per-fund state, number of delta values counted)."""

    records = json.loads(BASELINE.read_text(encoding="utf-8"))

    canonical = json.loads((REPOSITORY_ROOT / "data/input/funds.json").read_text(encoding="utf-8"))

    if isinstance(canonical, dict):
        canonical = canonical.get("funds", canonical)

    canonical_index = {
        (fund["name"] if isinstance(fund, dict) else fund): index
        for index, fund in enumerate(canonical)
    }

    state = []

    for index, record in enumerate(records):
        status = {field: (record.get(field) or {}).get("status", "MISSING") for field in FIELDS}

        state.append(
            {
                "name": record["name"],
                "web": record.get("web"),
                "canonical_index": canonical_index.get(record["name"], index),
                "status": status,
                "baseline_usable": sum(1 for field in FIELDS if status[field] != "MISSING"),
                "delta_added": {},
            }
        )

    by_name = {entry["name"]: entry for entry in state}

    applied_total = 0

    for delta_file in DELTAS:
        if not (EMERGENCY / delta_file).exists():
            continue

        delta = json.loads((EMERGENCY / delta_file).read_text(encoding="utf-8"))

        for value in delta.get("applied_values", []):
            entry = by_name.get(value["fund"])

            if entry is None:
                continue

            if value.get("scope_class") and value["scope_class"] not in COUNTED_SCOPE:
                continue

            already = value["field"] in entry["delta_added"]

            if entry["status"][value["field"]] == "MISSING" and not already:
                entry["delta_added"][value["field"]] = delta_file

                applied_total += 1

    for entry in state:
        entry["usable"] = entry["baseline_usable"] + len(entry["delta_added"])

        entry["missing"] = [
            field
            for field in FIELDS
            if entry["status"][field] == "MISSING" and field not in entry["delta_added"]
        ]

    return state, applied_total


def processed_funds():
    """Every fund any stage inspected, whether or not it yielded a value."""

    done = set()

    for delta_file in DELTAS:
        if not (EMERGENCY / delta_file).exists():
            continue

        delta = json.loads((EMERGENCY / delta_file).read_text(encoding="utf-8"))

        for entry in delta.get("per_fund", []):
            done.add(entry["fund"])

    return done


def sorted_order(state):
    """Poorest funds first, canonical order as the tie-break."""

    return sorted(
        range(len(state)),
        key=lambda index: (state[index]["baseline_usable"], state[index]["canonical_index"]),
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    state, applied = load()

    effective = sum(entry["usable"] for entry in state)

    print("baseline sha256:", baseline_sha256())

    print(
        "records:",
        len(state),
        "baseline usable:",
        sum(entry["baseline_usable"] for entry in state),
    )

    print("delta applied counted:", applied)

    print("effective usable:", effective, f"{effective / (len(state) * 11) * 100:.2f}%")

    print("processed funds:", len(processed_funds()))
