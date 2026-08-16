"""
Probe one document of either kind and grep it.

The workhorse of every stage: fetch through the cache chain, report the kind,
the page count and the character count, then print the lines around each
pattern. The header comes before the content on purpose - a PDF reporting
30+ pages and a few hundred characters has no text layer, and nothing read out
of it may be accepted.

TEMPORARY RECOVERY TOOL.

Usage::

    uv run python scripts/emergency/grep_document.py <url> [pattern ...]
"""

from __future__ import annotations

import re
import sys

import em

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    url = sys.argv[1]

    patterns = sys.argv[2:]

    meta, body = em.get(url)

    content_type = (meta.get("content_type") or "").lower()

    if "pdf" in content_type or body[:5] == b"%PDF-":
        try:
            text, pages = em.pdf_text(body)
        except Exception as error:  # noqa: BLE001 - a broken PDF is a result, not a crash
            print("PDF ERROR", error, meta)

            raise SystemExit(0) from None

        print(f"[PDF {meta['status']} pages={pages} chars={len(text)}] {meta['final_url']}")
    else:
        encoding = "cp1250" if "1250" in content_type else None

        text = em.html_text(body, encoding)

        print(f"[HTML {meta['status']} chars={len(text)}] {meta['final_url']}")

    if not patterns:
        print(text[:4000])

        raise SystemExit(0)

    lines = text.split("\n")

    hits: set[int] = set()

    for index, line in enumerate(lines):
        for pattern in patterns:
            if re.search(pattern, line, re.I):
                hits.update(range(max(0, index - 3), min(len(lines), index + 8)))

    previous = -2

    for index in sorted(hits):
        if index != previous + 1:
            print("  ...")

        print(f"{index:>5}| {lines[index]}")

        previous = index
