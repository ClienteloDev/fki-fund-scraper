"""Fetch one Sbirka listin deed into a batch-06 workspace and grep it.

The download URL is session-bound *and* expires: it must be obtained and read
inside the same httpx.Client, and the body must be cached at download time - a
re-fetch later returns a 1 037-byte „Neplatny odkaz" page that probes as a
0-page document (batch-05 finding 8).

    uv run python deed.py <workspace> <subjektId> <dokument> <spis> [pattern ...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "scripts" / "emergency"))

import b6  # noqa: E402
import em  # noqa: E402
import orsl2  # noqa: E402


def main():
    ws, subject, dokument, spis = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    patterns = sys.argv[5:]
    b6.use(ws)
    with orsl2.client() as c:
        url, body = orsl2.deed_pdf(c, subject, dokument, spis)
    if not url:
        print("no download link on the detail page")
        return
    try:
        text, pages = em.pdf_text(body)
    except Exception as error:  # noqa: BLE001
        print(f"[not a PDF: {error}] {url} bytes={len(body) if body else 0}")
        print((body or b"")[:400])
        return
    print(f"[pages={pages} chars={len(text)} bytes={len(body)} "
          f"quality={b6.probe_quality(text, pages)}] {url}")
    lines = text.split("\n")
    if not patterns:
        print("\n".join(lines[:120]))
        return
    hits: set[int] = set()
    for i, line in enumerate(lines):
        for p in patterns:
            if re.search(p, line, re.I):
                hits.update(range(max(0, i - 4), min(len(lines), i + 5)))
    prev = -2
    for i in sorted(hits):
        if i != prev + 1:
            print("  ...")
        print(f"{i:>5}| {lines[i]}")
        prev = i


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
