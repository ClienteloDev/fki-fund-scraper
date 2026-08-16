"""
Fetch one Sbirka-listin filing and read it, with the text layer probed first.

The whole register workflow end to end: seat the session, resolve the detail,
download the deed, and report ``pages`` and ``chars`` before printing anything.
That header is the point. A newest annual report filed as an ESEF package
serves a 0-page, ~94-character text layer, and an image-only scan returns
30-300 characters over 30+ pages. Either way the answer is to fall back one or
two years to the nearest readable filing and let ``as_of`` carry the staleness -
never to accept a value read out of a document with no text.

TEMPORARY RECOVERY TOOL. The rule it encodes - refuse a document under roughly
500 characters over 10+ pages and try the previous year - belongs in the
pipeline as a precondition everywhere, not in this script.

Usage::

    uv run python scripts/emergency/register_deed.py <subjektId> <dokument> <spis> [pattern]
"""

from __future__ import annotations

import re
import sys

import em
import orsl2

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    subject_id, dokument, spis = sys.argv[1], sys.argv[2], sys.argv[3]

    pattern = sys.argv[4] if len(sys.argv) > 4 else None

    with orsl2.client() as session:
        url, body = orsl2.deed_pdf(session, subject_id, dokument, spis)

    print("URL:", url, "bytes:", len(body) if body else None)

    if not body:
        raise SystemExit(0)

    text, pages = em.pdf_text(body)

    print(f"[pages={pages} chars={len(text)}]")

    if pages >= 10 and len(text) < 500:
        print(
            "  !! no usable text layer (image-only scan or ESEF package) - "
            "fall back to the previous year rather than reading this one"
        )

    if not pattern:
        print(text[:3000])

        raise SystemExit(0)

    lines = text.split("\n")

    hits: set[int] = set()

    for index, line in enumerate(lines):
        if re.search(pattern, line, re.I):
            hits.update(range(max(0, index - 2), min(len(lines), index + 9)))

    previous = -2

    for index in sorted(hits):
        if index != previous + 1:
            print("  ...")

        print(f"{index:>5}| {lines[index]}")

        previous = index
