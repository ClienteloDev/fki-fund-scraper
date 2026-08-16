"""Extract per-fund establishment / first-accounting-period evidence from the
programme's own records. No network, no new sources.

Sources scanned (all existing evidence):
  - the 18 emergency deltas and their records.jsonl
  - the five online-recovery batch deltas and records.jsonl
  - data/output/funds.delivery.current-enriched.json (evidence / condition / note text)
  - reports/online-recovery-batch-0*.md and ONLINE_RECOVERY_HANDOFF.md
"""

import json
import glob
import re
import collections
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Where the extracted evidence is written, and where rebuild_queue.py reads it from.
OUT = os.path.join(ROOT, "reports", "age-evidence.json")

MON = {
    "ledna": 1, "února": 2, "unora": 2, "března": 3, "brezna": 3, "dubna": 4,
    "května": 5, "kvetna": 5, "června": 6, "cervna": 6, "července": 7, "cervence": 7,
    "srpna": 8, "září": 9, "zari": 9, "října": 10, "rijna": 10, "listopadu": 11,
    "prosince": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

D = r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})"
DW = r"(\d{1,2})\s+(" + "|".join(k for k in MON if k.isalpha()) + r")\s+(\d{4})"


def _mk(d, mo, y):
    if not str(mo).isdigit():
        mo = MON[str(mo).lower()]
    return "%04d-%02d-%02d" % (int(y), int(mo), int(d))


ESTAB = [
    re.compile(r"vznik(?:l|la|lo|ly)\s+(?:dne\s+)?" + D, re.I),
    re.compile(r"vznik[:\s]+" + D, re.I),
    re.compile(r"zaps[áa]n(?:[ay])?(?:\s+do\s+(?:obchodn[íi]ho\s+rejst[řr][íi]ku|seznamu[^,;.]{0,30}))?\s+(?:dne\s+)?" + D, re.I),
    re.compile(r"den\s+z[áa]pisu[:\s]+" + D, re.I),
    re.compile(r"zalo[žz]en[ay]?\s+(?:dne\s+)?" + D, re.I),
    re.compile(r"entered\s+(?:in\s+the\s+register\s+|the\s+register\s+)?(?:on\s+)?" + DW, re.I),
    re.compile(r"entered\s+(?:in\s+the\s+register\s+|the\s+register\s+)?(?:on\s+)?" + D, re.I),
]
CZ_MONTH_LOC = (r"(?:lednu|[úu]noru|b[řr]eznu|dubnu|kv[ěe]tnu|[čc]ervnu|[čc]ervenci|srpnu|z[áa][řr][íi]"
                r"|[řr][íi]jnu|listopadu|prosinci)")
ESTAB_YEAR = [
    re.compile(r"fond\s+zaps[áa]n\s+(?:v\s+roce\s+)?(20\d{2})", re.I),
    re.compile(r"zaps[áa]n[ay]?\s+v\s+roce\s+(20\d{2})", re.I),
    # "Fond i podfond vznikly v únoru/březnu 2026"
    re.compile(r"vznik(?:l|la|lo|ly|li)\s+v\s+" + CZ_MONTH_LOC + r"(?:\s*/\s*" + CZ_MONTH_LOC + r")?\s+(20\d{2})", re.I),
    re.compile(r"zaps[áa]n[ay]?\s+v\s+" + CZ_MONTH_LOC + r"(?:\s*/\s*" + CZ_MONTH_LOC + r")?\s+(20\d{2})", re.I),
]

FIRST_PERIOD = re.compile(
    r"prvn[íi]\s*(?:\([^)]{0,30}\)\s*)?(?:[úu][čc]etn[íi])?\s*obdob[íi][^.;:]{0,140}?"
    r"(\d{1,2})\.\s*(\d{1,2})\.?\s*(\d{4})?\s*[–\-—]{1,2}\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})",
    re.I,
)
NO_PRIOR = re.compile(
    r"(srovn[áa]vac[íi]\s+(?:sloupec|obdob[íi]|hodnoty)[^.;]{0,70}(?:neexistuj|pr[áa]zdn|nulov|je\s*0|n/a|N/A)"
    r"|[žz][áa]dn[éaá]\s+(?:srovn[áa]vac[íi]\s+obdob[íi]|[úu][čc]etn[íi]\s+z[áa]v[ěe]rka)"
    r"|prvn[íi]\s+v[ýy]ro[čc]n[íi]\s+zpr[áa]va\s+(?:dosud\s+)?neexistuje"
    r"|jde\s+o\s+prvn[íi]\s+[úu][čc]etn[íi]\s+obdob[íi])",
    re.I,
)


def scan(blob, fund, src, est):
    for pat in ESTAB:
        for m in pat.finditer(blob):
            try:
                est[fund].append(("date", _mk(m.group(1), m.group(2), m.group(3)), src,
                                  blob[max(0, m.start() - 100):m.end() + 70]))
            except Exception:
                pass
    for pat in ESTAB_YEAR:
        for m in pat.finditer(blob):
            est[fund].append(("year", m.group(1), src, blob[max(0, m.start() - 100):m.end() + 70]))
    for m in FIRST_PERIOD.finditer(blob):
        y0 = m.group(3) or m.group(6)
        try:
            start = _mk(m.group(1), m.group(2), y0)
            end = _mk(m.group(4), m.group(5), m.group(6))
        except Exception:
            continue
        est[fund].append(("first_period", start + "/" + end, src,
                          blob[max(0, m.start() - 100):m.end() + 70]))
    if NO_PRIOR.search(blob):
        est[fund].append(("no_prior_period", "", src, ""))


def iter_records():
    for p in ["data/output/emergency/*records.jsonl", "data/output/recovery-online/*records.jsonl"]:
        for f in sorted(glob.glob(os.path.join(ROOT, p))):
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                yield os.path.basename(f), r
    for p in ["data/output/emergency/*delta.json", "data/output/recovery-online/*delta.json"]:
        for f in sorted(glob.glob(os.path.join(ROOT, p))):
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            stack = [d]
            while stack:
                x = stack.pop()
                if isinstance(x, dict):
                    if isinstance(x.get("fund"), str):
                        yield os.path.basename(f), x
                    stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)


def main():
    canon = [f["name"] for f in json.load(open(os.path.join(ROOT, "data/input/funds.json"), encoding="utf-8"))]
    est = collections.defaultdict(list)

    for src, r in iter_records():
        fund = r.get("fund") or r.get("fund_name")
        if not isinstance(fund, str):
            continue
        scan(json.dumps(r, ensure_ascii=False), fund, src, est)

    for rec in json.load(open(os.path.join(ROOT, "data/output/funds.delivery.current-enriched.json"), encoding="utf-8")):
        scan(json.dumps(rec, ensure_ascii=False), rec["name"], "current-enriched.json", est)

    # markdown write-ups: attribute a sentence to the nearest fund name mentioned
    md = []
    for p in ["reports/online-recovery-batch-0*.md", "reports/ONLINE_RECOVERY_HANDOFF.md",
              "reports/PRE_REFACTOR_HANDOFF.md", "reports/EMERGENCY_ENRICHMENT_FINAL.md",
              "reports/emergency-stage-*.md"]:
        md.extend(sorted(glob.glob(os.path.join(ROOT, p))))
    # index canonical names by a distinctive prefix token
    for f in md:
        text = open(f, encoding="utf-8").read()
        for name in canon:
            key = name.split(",")[0].split(" SICAV")[0].split(" investiční")[0].strip()
            if len(key) < 4:
                continue
            for m in re.finditer(re.escape(key), text):
                window = text[m.start():m.start() + 400]
                scan(window, name, os.path.basename(f), est)

    out = {}
    for fund, items in est.items():
        out[fund] = {
            "establishment_dates": sorted({v for k, v, _, _ in items if k == "date"}),
            "establishment_years": sorted({v for k, v, _, _ in items if k == "year"}),
            "first_periods": sorted({v for k, v, _, _ in items if k == "first_period"}),
            "no_prior_period": any(k == "no_prior_period" for k, *_ in items),
            "quotes": [q.replace("\n", " ")[:260] for k, v, s, q in items if k in ("date", "year", "first_period")][:5],
        }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("funds with any age evidence:", len(out))
    print("  with establishment date/year:", sum(1 for v in out.values() if v["establishment_dates"] or v["establishment_years"]))
    print("  with first-period statement :", sum(1 for v in out.values() if v["first_periods"]))
    print("  canonical-name matches      :", sum(1 for k in out if k in canon))


main()
