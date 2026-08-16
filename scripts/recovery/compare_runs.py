"""Scraper diagnostic pass: what each isolated run produced, and for which slots.

Only candidates for slots the database has **empty** count as a scraper gain; a
candidate for a populated slot is recorded but never applied.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Where runs.sh wrote the per-run logs, and where the raw comparison is emitted.
LOGS = ROOT / "cache" / "recovery-online" / "runs"
OUT = ROOT / "cache" / "recovery-online" / "scraper-raw.json"

FUNDS = [
    ("b06-01-convenio", "Convenio, investiční fond s proměnným základním kapitálem, a.s."),
    ("b06-02-good-value-investments", "Good Value Investments SICAV, a.s."),
    ("b06-03-creditas-assets", "CREDITAS ASSETS SICAV a.s."),
    ("b06-04-evermore-capital-management", "Evermore Capital Management a.s., SICAV"),
    ("b06-05-falanga-invest", "Falanga Invest SICAV a.s."),
    ("b06-06-energy-financial-group-fund", "Energy financial group Fund SICAV a.s."),
    ("b06-07-adversum", "Adversum SICAV, a.s."),
    ("b06-08-bhs-iconic-cars", "BHS ICONIC CARS SICAV, a.s."),
    ("b06-09-bidli", "BIDLI investiční fond SICAV, a.s."),
    ("b06-10-reticulum", "Reticulum Fund SICAV, a.s."),
]

FIELDS = ["investment_horizon", "minimum_investment", "target_return", "fees",
          "assets_under_management", "manager", "administrator", "aum_history",
          "annual_returns", "historical_values", "news"]


def status_of(block):
    if not isinstance(block, dict):
        return "MISSING"
    return block.get("status") or ("found" if block.get("value") else "MISSING")


def main():
    db = {r["name"]: r for r in json.load(
        open(ROOT / "data/output/funds.delivery.current-enriched.json", encoding="utf-8"))}
    runs, verdict_rows = [], []
    for ws, name in FUNDS:
        out = ROOT / "cache" / "recovery-online" / ws / "output" / "enriched.json"
        log = LOGS / f"{ws}.log"
        row = {"workspace": ws, "fund": name}
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            for key, pattern in (("pages", r"pages[_ ]?(?:visited|crawled)\D{0,12}(\d+)"),
                                 ("documents_found", r"documents?[_ ]?(?:found|discovered)\D{0,12}(\d+)"),
                                 ("downloaded", r"download(?:ed)?\D{0,12}(\d+)"),
                                 ("parsed", r"parsed\D{0,12}(\d+)"),
                                 ("failures", r"fail(?:ure|ed)s?\D{0,12}(\d+)")):
                m = re.search(pattern, text, re.I)
                if m:
                    row[key] = int(m.group(1))
            row["log_tail"] = "\n".join(text.strip().split("\n")[-25:])
        sq = ROOT / "cache" / "recovery-online" / ws / "db.sqlite3"
        if sq.exists():
            con = sqlite3.connect(f"file:{sq}?mode=ro", uri=True)
            try:
                for label, query in (("sources", "select count(*) from sources"),
                                     ("parsed_documents", "select count(*) from parsed_documents"),
                                     ("attempts_failed",
                                      "select count(*) from attempts where status not in ('ok','success')")):
                    try:
                        row[label] = con.execute(query).fetchone()[0]
                    except Exception:
                        pass
            finally:
                con.close()
        if out.exists():
            data = json.load(open(out, encoding="utf-8"))
            rec = next((r for r in data if r.get("name") == name), None)
            if rec:
                produced = {}
                for f in FIELDS:
                    st = status_of(rec.get(f))
                    if st != "MISSING":
                        produced[f] = rec[f]
                row["produced_fields"] = sorted(produced)
                for f, block in produced.items():
                    empty_in_db = status_of((db.get(name) or {}).get(f)) == "MISSING"
                    verdict_rows.append({
                        "fund": name, "field": f,
                        "slot_empty_in_database": empty_in_db,
                        "candidate": block,
                    })
        runs.append(row)

    json.dump({"runs": runs, "candidates": verdict_rows},
              open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"runs: {len(runs)}")
    for r in runs:
        print(f"  {r['workspace']:<36} sources={r.get('sources','-'):<6} parsed={r.get('parsed_documents','-'):<5} "
              f"produced={r.get('produced_fields')}")
    print(f"\ncandidates: {len(verdict_rows)}  "
          f"(for empty slots: {sum(1 for v in verdict_rows if v['slot_empty_in_database'])})")
    for v in verdict_rows:
        if v["slot_empty_in_database"]:
            print(f"  EMPTY-SLOT  {v['fund'][:40]:<42} {v['field']:<24} "
                  f"{json.dumps(v['candidate'], ensure_ascii=False)[:170]}")


main()
