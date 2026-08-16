"""What is already on disk for a fund: cache/regen.sqlite3 inventory only.

The database is inventory, never identity evidence. Every document listed here is
re-verified against the fund's name / ICO / ISIN before any value is taken from it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(pattern, show_all=False):
    c = sqlite3.connect(f"file:{ROOT / 'cache' / 'regen.sqlite3'}?mode=ro", uri=True)
    rows = list(c.execute("select fund_id, name from funds where name like ?", (f"%{pattern}%",)))
    for fund_id, name in rows:
        print("=" * 100)
        print(f"fund_id={fund_id}  {name}")
        docs = list(c.execute("""
            select s.url, s.document_type, s.http_status, s.status,
                   p.page_count, p.character_count, p.text_path,
                   m.subfund_name, m.reporting_period_end, m.published_at, m.matched_ico, m.scope
            from sources s
            left join parsed_documents p on p.source_id = s.source_id
            left join document_metadata m on m.source_id = s.source_id
            where s.fund_id = ? and (s.local_path is not null or p.source_id is not null)
            order by coalesce(m.reporting_period_end, s.published_at, '') desc, s.url
        """, (fund_id,)))
        print(f"  cached documents: {len(docs)}")
        for u, dt, http, st, pc, cc, tp, sub, rpe, pub, ico, scope in docs:
            if not show_all and (pc is None):
                continue
            flag = ""
            if pc and pc >= 10 and (cc or 0) < 500:
                flag = "  <<IMAGE-ONLY?"
            print(f"   [{dt or '-':<16}] pages={pc or '-':<4} chars={cc or '-':<7} "
                  f"period={rpe or '-':<11} sub={(sub or '-')[:22]:<24}{flag}")
            print(f"       {u[:150]}")


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main(sys.argv[1], "--all" in sys.argv)
