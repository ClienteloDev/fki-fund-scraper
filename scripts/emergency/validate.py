"""
Independent validation of the consolidated outputs, re-read from disk.

Deliberately does **not** import ``consolidate``. It reloads the baseline, the
canonical input, the 18 deltas and both current outputs and checks the
properties the consolidation claims, so a defect in the consolidation cannot
hide behind the consolidation's own assertions.

What it asserts:

- both outputs are valid JSON with 341 records in canonical order;
- the baseline is still byte-identical to its recorded sha256;
- every delta on disk is applied, and no (fund, field) twice;
- no baseline value was overwritten, no populated slot deleted, and no value
  appears that no delta authorises;
- every emergency value carries a source and evidence;
- the tier counts add up and the verified projection agrees with the enriched
  file;
- the verified projection contains only ``found`` / ``not_found``.

Exits non-zero on the first failing property, so it can be used as a gate.

TEMPORARY RECOVERY TOOL - but this is the check to run before trusting any
future regeneration of the database.

Usage::

    uv run python scripts/emergency/validate.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

OUTPUT = REPOSITORY_ROOT / "data/output"

EMERGENCY = OUTPUT / "emergency"

BASELINE_NAME = "funds.delivery.offline-recovery1-enriched.json"

BASELINE_SHA = "6304c1cf203fdd08c5aad6e4bd7bcc746cdf5e3b47895dfde13246ec0b129242"

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

COUNTED = {"FUND", "FUND_UNIFORM", "SOLE_SUBFUND"}

DELTAS = [
    "worker-1-sample-delta.json",
    "stage-01-part2-delta.json",
    *[f"stage-{index:02d}-delta.json" for index in range(2, 18)],
]

EXPECTED_USABLE = 1966


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    passed, failed = [], []

    def check(name, condition, detail=""):
        (passed if condition else failed).append(f"{name}{(' — ' + detail) if detail else ''}")

    enriched = load(OUTPUT / "funds.delivery.current-enriched.json")

    verified = load(OUTPUT / "funds.delivery.current-verified.json")

    baseline = load(OUTPUT / BASELINE_NAME)

    canonical = load(REPOSITORY_ROOT / "data/input/funds.json")

    if isinstance(canonical, dict):
        canonical = canonical.get("funds", canonical)

    check("JSON validity (4 files parse)", True)

    expected_funds = len(canonical)

    check(
        f"{expected_funds} funds in enriched",
        len(enriched) == expected_funds,
        str(len(enriched)),
    )

    check(
        f"{expected_funds} funds in verified",
        len(verified) == expected_funds,
        str(len(verified)),
    )

    names = [fund["name"] if isinstance(fund, dict) else fund for fund in canonical]

    check("canonical order in enriched", [record["name"] for record in enriched] == names)

    check("canonical order in verified", [record["name"] for record in verified] == names)

    check(
        "baseline sha256 unchanged",
        hashlib.sha256((OUTPUT / BASELINE_NAME).read_bytes()).hexdigest() == BASELINE_SHA,
    )

    on_disk = sorted(path.name for path in EMERGENCY.glob("*-delta.json"))

    check(
        "every delta on disk is applied",
        sorted(DELTAS) == on_disk,
        f"disk={len(on_disk)} applied={len(DELTAS)}",
    )

    check("no duplicate stage", len(set(DELTAS)) == len(DELTAS))

    expected = {}

    seen_pairs = set()

    duplicates = []

    for delta_file in DELTAS:
        delta = load(EMERGENCY / delta_file)

        for value in delta.get("applied_values", []):
            pair = (value["fund"], value["field"])

            scope_class = value.get("scope_class")

            if scope_class is not None and scope_class not in COUNTED:
                continue

            if pair in seen_pairs:
                duplicates.append((delta_file, pair))

            seen_pairs.add(pair)

            expected.setdefault(pair, delta_file)

    check(
        "no duplicate application of the same (fund, field)",
        not duplicates,
        str(duplicates[:3]),
    )

    by_name = {record["name"]: record for record in baseline}

    overwrites, deletions, unauthorised = [], [], []

    for record in enriched:
        before = by_name[record["name"]]

        for field in FIELDS:
            baseline_status = (before.get(field) or {}).get("status", "MISSING")

            now = record.get(field) or {}

            if baseline_status != "MISSING":
                if now != before.get(field):
                    overwrites.append((record["name"], field))
            elif now.get("status", "MISSING") != "MISSING":
                if (record["name"], field) not in expected:
                    unauthorised.append((record["name"], field))
            elif (record["name"], field) in expected:
                deletions.append((record["name"], field))

    check("no unauthorised overwrite of a baseline value", not overwrites, str(overwrites[:3]))

    check("no accidental value deletion", not deletions, str(deletions[:3]))

    check("no value without a delta authorising it", not unauthorised, str(unauthorised[:3]))

    def is_emergency(slot):
        return ((slot or {}).get("provenance") or {}).get("origin") == "emergency_enrichment"

    unprovenanced = [
        (record["name"], field)
        for record in enriched
        for field in FIELDS
        if is_emergency(record.get(field))
        and not (record[field].get("source_url") and record[field].get("evidence"))
    ]

    check("every emergency value has source + evidence", not unprovenanced, str(unprovenanced[:3]))

    emergency_slots = sum(
        1 for record in enriched for field in FIELDS if is_emergency(record.get(field))
    )

    check(
        "emergency slots == counted applied values",
        emergency_slots == len(expected),
        f"{emergency_slots} vs {len(expected)}",
    )

    usable = sum(
        1
        for record in enriched
        for field in FIELDS
        if (record.get(field) or {}).get("status", "MISSING") != "MISSING"
    )

    found = sum(1 for record in verified for field in FIELDS if record[field]["status"] == "found")

    check(f"usable == {EXPECTED_USABLE}", usable == EXPECTED_USABLE, str(usable))

    check("verified projection agrees with enriched", found == usable, f"{found} vs {usable}")

    baseline_usable = sum(
        1
        for record in baseline
        for field in FIELDS
        if (record.get(field) or {}).get("status", "MISSING") != "MISSING"
    )

    check(
        "baseline + applied == usable",
        baseline_usable + len(expected) == usable,
        f"{baseline_usable}+{len(expected)} vs {usable}",
    )

    bad = [
        (record["name"], field)
        for record in verified
        for field in FIELDS
        if record[field]["status"] not in ("found", "not_found")
    ]

    check("verified statuses are found/not_found only", not bad, str(bad[:3]))

    for name in (
        "funds.delivery.current-enriched.json",
        "funds.delivery.current-verified.json",
    ):
        print(f"sha256  {name}  {hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest()}")

    print()

    for line in passed:
        print("PASS  " + line)

    for line in failed:
        print("FAIL  " + line)

    print(f"\n{len(passed)} passed, {len(failed)} failed")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    raise SystemExit(main())
