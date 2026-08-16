"""AVANT's fund-filtered dated announcement channel.

`oznameni-sitemap.xml` and `vyzvy-sitemap.xml` list every notice AVANT publishes.
Each item page carries a JSON-LD `datePublished` and its title carries the fund's
exact legal name, so the channel can be filtered by fund - which a group newsroom
cannot. This is what produced `news` for EDUCA, 4 Gimel and Ceskomoravsky.

    uv run python avantnews.py <workspace> <slug-fragment> [max items]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "scripts" / "emergency"))

import b6  # noqa: E402

SITEMAPS = [
    "https://www.avantfunds.cz/oznameni-sitemap.xml",
    "https://www.avantfunds.cz/vyzvy-sitemap.xml",
    "https://www.avantfunds.cz/napsali_o_nas-sitemap.xml",
]


def urls():
    out = []
    for sm in SITEMAPS:
        _meta, body = b6.get(sm)
        for m in re.finditer(r"<loc>([^<]+)</loc>", body.decode("utf-8", "replace")):
            out.append(m.group(1))
    return out


def date_of(url):
    _meta, body = b6.get(url)
    page = body.decode("utf-8", "replace")
    m = re.search(r'"datePublished"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})', page)
    if m:
        return m.group(1), "json-ld datePublished"
    m = re.search(r'property="article:published_time" content="([0-9]{4}-[0-9]{2}-[0-9]{2})', page)
    if m:
        return m.group(1), "og article:published_time"
    return None, None


def title_of(url):
    _meta, body = b6.get(url)
    page = body.decode("utf-8", "replace")
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def main():
    ws, frag = sys.argv[1], sys.argv[2].lower()
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    b6.use(ws)
    all_urls = urls()
    hits = [u for u in all_urls if frag in u.lower()]
    print(f"channel items: {len(all_urls)}   matching '{frag}': {len(hits)}")
    for u in hits[:limit]:
        d, origin = date_of(u)
        print(f"  {d or '     -    '}  [{origin or '-'}]  {u}")
        print(f"      {title_of(u)[:160]}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
