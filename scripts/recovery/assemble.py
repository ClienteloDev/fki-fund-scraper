"""Assemble the batch-06 delta and records from the per-fund findings files.

Hard invariants, asserted at build time:
  * every applied value targets a slot whose status in
    data/output/funds.delivery.current-enriched.json is MISSING;
  * the database file itself is not written to;
  * every applied value carries source_url, evidence, scope_class and confidence.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Per-fund findings written by hand during the batch, and the scraper verdicts.
FINDINGS = ROOT / "cache" / "recovery-online" / "findings"
SCRAPER_VERDICTS = ROOT / "cache" / "recovery-online" / "scraper-verdicts.json"
OUT = ROOT / "data" / "output" / "recovery-online"

FIELDS = ["investment_horizon", "minimum_investment", "target_return", "fees",
          "assets_under_management", "manager", "administrator", "aum_history",
          "annual_returns", "historical_values", "news"]


def main():
    db_path = ROOT / "data" / "output" / "funds.delivery.current-enriched.json"
    db_bytes = db_path.read_bytes()
    db_sha = hashlib.sha256(db_bytes).hexdigest()
    db = {r["name"]: r for r in json.loads(db_bytes.decode("utf-8"))}

    findings = []
    for path in sorted(glob.glob(str(FINDINGS / "*.json"))):
        findings.append(json.load(open(path, encoding="utf-8")))

    applied, not_applied, flags = [], [], []
    problems = []
    for f in findings:
        fund = f["fund"]
        rec = db.get(fund)
        if rec is None:
            problems.append(f"fund not in database: {fund}")
            continue
        for value in f.get("applied", []):
            field = value["field"]
            status = (rec.get(field) or {}).get("status")
            if status != "MISSING":
                problems.append(f"{fund} / {field}: slot is {status}, not MISSING — would overwrite")
            for required in ("value", "scope_class", "confidence", "source_url", "evidence"):
                if not value.get(required):
                    problems.append(f"{fund} / {field}: missing {required}")
            applied.append({"fund": fund, **value})
        for value in f.get("not_applied", []):
            not_applied.append({"fund": fund, **value})
        for flag in f.get("audit_flags", []):
            flag = dict(flag)
            flag["flag_kind"] = flag.pop("kind", None)
            flags.append({"fund": fund, **flag})

    if problems:
        print("BUILD REFUSED:")
        for p in problems:
            print("  -", p)
        return

    scraper = json.load(open(SCRAPER_VERDICTS, encoding="utf-8")) \
        if SCRAPER_VERDICTS.exists() else {"verdicts": [], "runs": []}

    delta = {
        "batch": "online-recovery-06",
        "generated_at": "2026-08-16",
        "queue": "reports/ONLINE_RECOVERY_QUEUE_v2.csv (age-corrected); batch-06 = the top 10 unprocessed / retry-approved rows",
        "database": "data/output/funds.delivery.current-enriched.json",
        "database_sha256": db_sha,
        "database_modified": False,
        "funds": [{"fund": f["fund"], "batch_rank": f["batch_rank"],
                   "queue_rank_v2": f["queue_rank_v2"], "queue_rank_v1": f["queue_rank_v1"],
                   "workspace": f["workspace"], "register": f["register"],
                   "structure": f["structure"],
                   "gain": len(f.get("applied", []))} for f in findings],
        "applied": applied,
        "not_applied": not_applied,
        "audit_flags": flags,
        "scraper": scraper,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "batch-06-delta.json").write_text(
        json.dumps(delta, ensure_ascii=False, indent=1), encoding="utf-8")

    with open(OUT / "batch-06-records.jsonl", "w", encoding="utf-8") as fh:
        for value in applied:
            fh.write(json.dumps({"kind": "applied", **value}, ensure_ascii=False) + "\n")
        for value in not_applied:
            fh.write(json.dumps({"kind": "not_applied", **value}, ensure_ascii=False) + "\n")
        for flag in flags:
            fh.write(json.dumps({"kind": "audit_flag", **flag}, ensure_ascii=False) + "\n")
        for v in scraper.get("verdicts", []):
            fh.write(json.dumps({"kind": "scraper_verdict", **v}, ensure_ascii=False) + "\n")
        for r in scraper.get("runs", []):
            fh.write(json.dumps({"kind": "run_diagnostic", **r}, ensure_ascii=False) + "\n")

    # metrics
    import collections
    by_field = collections.Counter(v["field"] for v in applied)
    by_scope = collections.Counter(v["scope_class"] for v in applied)
    by_conf = collections.Counter(v["confidence"] for v in applied)
    by_acq = collections.Counter(v.get("acquisition", "?").split(" (")[0] for v in applied)
    by_fund = collections.Counter(v["fund"] for v in applied)
    by_verdict = collections.Counter(v["verdict"] for v in not_applied)
    review = sum(1 for v in applied if v.get("review_required"))

    usable_before = sum(1 for rec in db.values() for f in FIELDS
                        if (rec.get(f) or {}).get("status") not in (None, "MISSING"))
    total_slots = len(db) * len(FIELDS)
    print(f"database sha256 {db_sha}")
    print(f"usable before   {usable_before} / {total_slots} ({usable_before / total_slots:.2%})")
    print(f"applied         {len(applied)}   (review_required {review})")
    print(f"not applied     {len(not_applied)}")
    print(f"audit flags     {len(flags)}")
    print("by fund   ", dict(by_fund))
    print("by field  ", dict(by_field))
    print("by scope  ", dict(by_scope))
    print("by conf   ", dict(by_conf))
    print("by acq    ", dict(by_acq))
    print("by verdict", dict(by_verdict))
    print(f"projected after batch 06 alone: {usable_before + len(applied)} "
          f"({(usable_before + len(applied)) / total_slots:.2%})")


main()
