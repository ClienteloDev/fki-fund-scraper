"""Batch-06 pre-check: legal status, date of entry, historic names, board,
subfonds and the Sbirka listin filing list for the ten funds.

One STARTS_WITH search per fund on the full legal-name prefix, then the uplny
vypis and the filing listing inside the same session (register detail links are
session-bound).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "emergency"))
sys.path.insert(0, str(HERE))

# Register pre-check output: the uplny vypis text per fund and the aggregate JSON.
OUT = ROOT / "cache" / "recovery-online" / "precheck"

import b6  # noqa: E402
import em  # noqa: E402
import orsl2  # noqa: E402
import register_search  # noqa: E402

FUNDS = [
    ("b06-01", "convenio", "Convenio, investiční fond s proměnným základním kapitálem, a.s.", "Convenio"),
    ("b06-02", "good-value-investments", "Good Value Investments SICAV, a.s.", "Good Value Investments"),
    ("b06-03", "creditas-assets", "CREDITAS ASSETS SICAV a.s.", "CREDITAS ASSETS"),
    ("b06-04", "evermore-capital-management", "Evermore Capital Management a.s., SICAV", "Evermore Capital"),
    ("b06-05", "falanga-invest", "Falanga Invest SICAV a.s.", "Falanga Invest"),
    ("b06-06", "energy-financial-group-fund", "Energy financial group Fund SICAV a.s.", "Energy financial group Fund"),
    ("b06-07", "adversum", "Adversum SICAV, a.s.", "Adversum SICAV"),
    ("b06-08", "bhs-iconic-cars", "BHS ICONIC CARS SICAV, a.s.", "BHS ICONIC CARS"),
    ("b06-09", "bidli-investicni-fond", "BIDLI investiční fond SICAV, a.s.", "BIDLI investiční fond"),
    ("b06-10", "reticulum-fund", "Reticulum Fund SICAV, a.s.", "Reticulum Fund"),
]

FIELDS = [
    ("den zápisu", r"Datum vzniku a zápisu[:\s]*([0-9]{1,2}\.\s*[a-zěščřžýáíéůúň]+\s*[0-9]{4})"),
    ("v likvidaci", r"(v likvidaci)"),
]


def summarise(full_text):
    out = {}
    m = re.search(r"Datum vzniku a zápisu:\s*([0-9]{1,2}\.\s*[^\n]{3,25}?[0-9]{4})", full_text)
    out["entry_date"] = m.group(1).strip() if m else None
    m = re.search(r"Spisová značka:\s*([^\n]{3,60})", full_text)
    out["file_number"] = m.group(1).strip() if m else None
    m = re.search(r"Identifikační číslo:\s*([0-9 ]{8,12})", full_text)
    out["ico"] = m.group(1).replace(" ", "").strip() if m else None
    names = re.findall(r"Obchodní firma:\s*([^\n]{5,120})", full_text)
    out["names"] = list(dict.fromkeys(n.strip() for n in names))[:8]
    out["liquidation"] = bool(re.search(r"v likvidaci|Likvidace", full_text))
    board = re.findall(r"(?:člen představenstva|Statutární ředitel|Člen správní rady)[^\n]{0,10}\n?\s*([^\n]{5,100})",
                       full_text)
    out["board"] = list(dict.fromkeys(b.strip() for b in board))[:6]
    return out


def main():
    only = sys.argv[1:] or None
    result = {}
    for rank, slug, legal, prefix in FUNDS:
        if only and slug not in only:
            continue
        ws = f"{rank}-{slug}"
        b6.use(ws)
        print("=" * 100)
        print(f"{rank} {legal}")
        rec = {"legal_name": legal, "workspace": ws}
        with orsl2.client() as c:
            hits = register_search.search(c, prefix)
            rec["hits"] = [{"subjektId": s, "row": t} for s, t in hits]
            for s, t in hits:
                print(f"   hit subjektId={s} | {t}")
            if not hits:
                hits = register_search.search(c, prefix, contains=True, all_=True)
                rec["hits_contains"] = [{"subjektId": s, "row": t} for s, t in hits]
                for s, t in hits:
                    print(f"   [CONTAINS/VSECHNY] subjektId={s} | {t}")
            pick = None
            for s, t in hits:
                if "SICAV" in t or "investiční fond" in t or "proměnným" in t:
                    pick = s
                    break
            if pick is None and hits:
                pick = hits[0][0]
            rec["subjektId"] = pick
            if pick:
                url = register_search.full_extract_url(pick)
                meta, body = em.get(url)
                full = em.html_text(body)
                OUT.mkdir(parents=True, exist_ok=True)
                (OUT / f"uplny-{slug}.txt").write_text(full, encoding="utf-8")
                rec["uplny_vypis_url"] = url
                rec["summary"] = summarise(full)
                print("   ", json.dumps(rec["summary"], ensure_ascii=False)[:600])
                rows = register_search.listing(c, pick)
                rec["filings"] = [{"dokument": d, "spis": sp, "row": tx} for d, sp, tx in rows]
                print(f"    filings: {len(rows)}")
                for d, sp, tx in rows[:40]:
                    print(f"      dokument={d} spis={sp} | {tx[:190]}")
        result[legal] = rec
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "precheck.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    old.update(result)
    path.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")


main()
