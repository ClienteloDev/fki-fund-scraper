"""Batch-06 queue correction: add a fund-age penalty to the existing scoring formula.

The formula is unchanged apart from one new multiplicative factor:

    expected = expected_raw * route_quality * penalties * bonuses * AGE_PENALTY
    score    = expected / expected_minutes

AGE_PENALTY is derived, per fund, from the establishment / first-accounting-period
evidence the programme already holds, and it is field-aware: it removes exactly the
expected value of the financial fields that cannot exist because the fund has no
closed accounting period to report.

    age_penalty = expected_raw_after_age_blocking / expected_raw

so a fund with no age evidence, or a fund whose remaining missing fields are not the
age-blocked ones, keeps age_penalty = 1.0 and its rank is untouched.
"""

import csv
import json
import os
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Input written by extract_ages.py, and the directory the queue is emitted into.
# Both may be overridden for a dry run:
#     uv run python scripts/recovery/rebuild_queue.py [out_dir] [age_evidence.json]
AGE_EVIDENCE = os.path.join(ROOT, "reports", "age-evidence.json")
OUT_DIR = os.path.join(ROOT, "reports")
TODAY = "2026-08-16"

W = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.15, "UNLIKELY": 0.0}

# Fields whose recoverability depends on a closed accounting period existing.
# Multipliers per age class, calibrated on what batches 03-05 actually returned for
# funds of each age (batch 05: five 2025-registered funds returned 0 aum_history,
# 0 annual_returns, 0 historical_values and 1 AUM out of 5; batch 03: ASCAVIA and
# AVANT BESS, both 2025, returned statute fields only; batch 04: GAMA and JTFG II,
# both early 2024, returned full financial sets).
AGE_MULT = {
    "NO_CLOSED_PERIOD": {  # established 2026 - nothing has closed yet
        "assets_under_management": 0.15,
        "aum_history": 0.0,
        "annual_returns": 0.0,
        "historical_values": 0.0,
    },
    "ONE_STUB_PERIOD": {  # established 2025 - one partial period, no comparative
        "assets_under_management": 0.60,
        "aum_history": 0.0,
        "annual_returns": 0.0,
        "historical_values": 0.15,
    },
    "TWO_PERIODS": {  # established 2024 - one stub + one full period
        "assets_under_management": 1.0,
        "aum_history": 0.60,
        "annual_returns": 0.25,
        "historical_values": 0.70,
    },
    "MATURE": {},
}

# IČO bands, anchored on funds for which both the IČO and the establishment date are
# already known (JTPEG 19466340/2023-06 ... AGROCORE 24106020/2025-12). Above
# 24 000 000 the current sequence collides with the 2009-2014 block (Budějovická
# 24261386, INFOND 24207543, TTP 24141224 are all old), so that band is not used.
ICO_BANDS = [(20_900_000, 22_500_000, 2024), (22_500_000, 24_000_000, 2025)]


def load_age_evidence():
    return json.load(open(AGE_EVIDENCE, encoding="utf-8"))


def load_ico():
    ico = {}
    for r in csv.DictReader(open(os.path.join(ROOT, "reports/missing-data-by-fund.csv"), encoding="utf-8-sig")):
        if r["ico"].strip():
            ico[r["fund"]] = r["ico"].strip()
    for r in json.load(open(os.path.join(ROOT, "reports/cnb-match.json"), encoding="utf-8"))["matches"]:
        if r.get("cnb_ico"):
            ico.setdefault(r["canonical_name"], str(r["cnb_ico"]))
    return ico


def load_db():
    return {r["name"]: r for r in
            json.load(open(os.path.join(ROOT, "data/output/funds.delivery.current-enriched.json"), encoding="utf-8"))}


def has_multiyear_series(rec):
    """Positive counter-evidence: the fund demonstrably has >= 2 closed periods."""
    for field in ("aum_history", "historical_values", "annual_returns"):
        blk = rec.get(field)
        if not isinstance(blk, dict) or blk.get("status") == "MISSING":
            continue
        years = set()
        stack = [blk.get("value")]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("as_of", "year", "period_end") and isinstance(v, (str, int)):
                        years.add(str(v)[:4])
                    else:
                        stack.append(v)
            elif isinstance(x, list):
                stack.extend(x)
        if len({y for y in years if y.isdigit()}) >= 2:
            return True
    return False


def classify(fund, age, ico, db):
    """Return (age_class, established_year, evidence_kind, evidence)."""
    rec = db.get(fund, {})
    ev = age.get(fund, {})
    dates = ev.get("establishment_dates") or []
    years = ev.get("establishment_years") or []
    periods = ev.get("first_periods") or []

    year = None
    kind = None
    note = ""
    if periods:
        # The end of the first accounting period is the sharper signal: a prolonged
        # first period (Accolade 20. 12. 2024 - 31. 12. 2025) closes once, not twice.
        # `year` below is an *effective* establishment year, chosen so that the class
        # mapping further down (<=2023 mature / 2024 two / 2025 one stub / >=2026 none)
        # produces the right number of closed periods.
        end = min(periods).split("/")[1]
        if end <= "2025-12-31":
            year = int(end[:4])
        elif end <= TODAY:
            year = 2025
        else:
            year = 2026
        kind = "recorded_first_accounting_period"
        note = min(periods)
    elif dates:
        year = int(min(dates)[:4])
        kind = "recorded_establishment_date"
        note = min(dates)
    elif years:
        year = int(min(years))
        kind = "recorded_establishment_year"
        note = min(years)
    elif fund in ico and ico[fund].isdigit():
        n = int(ico[fund])
        for lo, hi, y in ICO_BANDS:
            if lo <= n < hi:
                year = y
                kind = "ico_band"
                note = "IČO %s" % ico[fund]
                break

    if year is None:
        return "MATURE", None, "no_age_evidence", ""

    if year <= 2023:
        return "MATURE", year, kind, note

    # positive counter-evidence wins: a printed multi-year series proves closed periods
    if has_multiyear_series(rec):
        return "MATURE", year, kind + "+existing_multiyear_series", note

    if year >= 2026:
        cls = "NO_CLOSED_PERIOD"
    elif year == 2025:
        cls = "ONE_STUB_PERIOD"
    else:
        cls = "TWO_PERIODS"
    return cls, year, kind, note


def main():
    q = list(csv.DictReader(open(os.path.join(ROOT, "reports/ONLINE_RECOVERY_QUEUE.csv"), encoding="utf-8-sig")))
    opp = collections.defaultdict(dict)
    for r in csv.DictReader(open(os.path.join(ROOT, "reports/recovery-opportunities.csv"), encoding="utf-8-sig")):
        opp[r["fund"]][r["field"]] = r["recoverability"]

    age = load_age_evidence()
    ico = load_ico()
    db = load_db()

    rows = []
    for r in q:
        fund = r["fund"]
        fields = [f for f in r["missing_fields"].split(";") if f]
        ratings = opp.get(fund, {})
        raw = sum(W[ratings.get(f, "UNLIKELY")] for f in fields)
        cls, year, kind, note = classify(fund, age, ico, db)
        mult = AGE_MULT[cls]
        raw_age = sum(W[ratings.get(f, "UNLIKELY")] * mult.get(f, 1.0) for f in fields)
        age_penalty = 1.0 if raw <= 0 else round(raw_age / raw, 4)
        blocked = sorted(f for f in fields if mult.get(f, 1.0) < 1.0)

        rq, pen, bon = float(r["route_quality"]), float(r["penalty_factor"]), float(r["bonus_factor"])
        expected_old = raw * rq * pen * bon
        expected_new = raw_age * rq * pen * bon
        minutes = float(r["expected_minutes"])
        rows.append(dict(
            old_rank=int(r["queue_rank"]), fund=fund, usable=int(r["usable"]), missing=int(r["missing"]),
            missing_fields=r["missing_fields"], official_routes=r["official_routes"],
            canonical_web_status=r["canonical_web_status"], ico=r["ico"] or ico.get(fund, ""),
            web=r["web"], high=int(r["high"]), medium=int(r["medium"]), low=int(r["low"]),
            expected_raw_slots=raw, route_quality=float(r["route_quality"]),
            penalty_factor=float(r["penalty_factor"]), bonus_factor=float(r["bonus_factor"]),
            age_class=cls, established_year=year or "", age_evidence_kind=kind, age_evidence=note,
            age_blocked_fields=";".join(blocked), age_penalty=age_penalty,
            expected_recoverable_fields_old=round(expected_old, 4),
            expected_recoverable_fields=round(expected_new, 4),
            expected_minutes=minutes,
            _score=expected_new / minutes,
            score_values_per_minute=round(expected_new / minutes, 4),
            reason=r["reason"], proven_hosts=r["proven_hosts"],
            cached_documents=int(r["cached_documents"]), cached_unexploited=int(r["cached_unexploited"]),
            slug=r["slug"], fund_index=int(r["fund_index"]),
        ))

    rows.sort(key=lambda x: (-x["_score"], -x["expected_recoverable_fields"], x["fund_index"]))
    for i, x in enumerate(rows, 1):
        x["queue_rank"] = i
        x.pop("_score")

    # --- eligibility -------------------------------------------------------
    # batch 01 processed ten funds under the canonical-web-only policy; nine of them
    # are closed (BAD_CANONICAL_URL / TRULY_EXHAUSTED / BLOCKED_IDENTITY / done in
    # batch 02).  MTK Invest is the one explicitly classified RETRY_WITH_OFFICIAL_ROUTE
    # with an untried route (the register), so it stays eligible.
    batch01_closed = [
        "SPM GROUP", "OneCap Private Equity", "OneCap Real Estate", "MAVERICK Fund",
        "OFO Project TWO", "TTP SICAV", "InnoBridge", "Green Ruby", "Ta Meri Invest",
    ]
    batch06 = {
        "Convenio, investiční fond s proměnným základním kapitálem, a.s.",
        "Good Value Investments SICAV, a.s.", "CREDITAS ASSETS SICAV a.s.",
        "Evermore Capital Management a.s., SICAV", "Falanga Invest SICAV a.s.",
        "Energy financial group Fund SICAV a.s.", "Adversum SICAV, a.s.",
        "BHS ICONIC CARS SICAV, a.s.", "BIDLI investiční fond SICAV, a.s.",
        "Reticulum Fund SICAV, a.s.",
    }
    processed = set(batch06)
    for r in q:
        if int(r["queue_rank"]) <= 40:
            processed.add(r["fund"])
    for x in rows:
        b01 = any(x["fund"].startswith(p) for p in batch01_closed)
        x["processed"] = "yes" if (x["fund"] in processed or b01) else "no"
        x["eligible"] = "no" if x["processed"] == "yes" else "yes"

    cols = ["queue_rank", "old_rank", "eligible", "processed",
            "fund", "usable", "missing", "missing_fields", "official_routes",
            "canonical_web_status", "expected_recoverable_fields", "score_values_per_minute",
            "age_class", "established_year", "age_evidence_kind", "age_evidence", "age_blocked_fields",
            "age_penalty", "expected_recoverable_fields_old", "reason", "fund_index", "ico", "web",
            "high", "medium", "low", "expected_raw_slots", "route_quality", "penalty_factor",
            "bonus_factor", "expected_minutes", "proven_hosts", "cached_documents",
            "cached_unexploited", "slug"]
    out_csv = os.path.join(OUT_DIR, "ONLINE_RECOVERY_QUEUE_v2.csv")
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for x in rows:
            w.writerow({c: x.get(c, "") for c in cols})

    json.dump({
        "built_from": "reports/ONLINE_RECOVERY_QUEUE.csv + reports/recovery-opportunities.csv",
        "correction": "fund-age penalty (batch 06)",
        "as_of": TODAY,
        "ordering": "score_values_per_minute desc, expected_recoverable_fields desc, fund_index asc",
        "age_multipliers": AGE_MULT,
        "ico_bands": ICO_BANDS,
        "fund_count": len(rows),
        "queue": rows,
    }, open(os.path.join(OUT_DIR, "ONLINE_RECOVERY_QUEUE_v2.json"), "w", encoding="utf-8"),
        ensure_ascii=False, indent=1)

    c = collections.Counter(x["age_class"] for x in rows)
    print("age classes:", dict(c))
    print("penalised funds (age_penalty < 1):", sum(1 for x in rows if x["age_penalty"] < 1))
    moved = sum(1 for x in rows if x["queue_rank"] != x["old_rank"])
    print("rank changed:", moved)
    print()
    print("NEW TOP 12 ELIGIBLE (unprocessed / retry-approved)")
    elig = [x for x in rows if x["eligible"] == "yes"]
    for x in elig[:12]:
        print(f"{x['queue_rank']:>3} (was {x['old_rank']:>3})  {x['fund'][:46]:<48} "
              f"{x['usable']}/11 miss={x['missing']:<2} exp={x['expected_recoverable_fields']:>6} "
              f"score={x['score_values_per_minute']:>6}  {x['age_class']}")
    print()
    print("PENALISED FUNDS (age_penalty < 1), by new rank")
    for x in rows:
        if x["age_penalty"] < 1:
            print(f"{x['queue_rank']:>3} (was {x['old_rank']:>3}) [{x['eligible']}] {x['fund'][:42]:<44} "
                  f"{x['age_class']:<17} y={x['established_year']} pen={x['age_penalty']:<6} "
                  f"{x['age_evidence_kind']} {x['age_evidence']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        OUT_DIR = sys.argv[1]
        os.makedirs(OUT_DIR, exist_ok=True)
    if len(sys.argv) > 2:
        AGE_EVIDENCE = sys.argv[2]
    main()
