"""
Probe an HTML page for its dated links - the newsroom and document probe.

Prints the length, the opening text and every link whose label carries a
printed date or reads like a newsroom or a document section. ``news`` is the
field with the largest remaining gap (292 missing of 341), and the rule that
governs it is strict: an item counts only when the listing prints a **date**,
the page is the **fund's own** domain, and the items are the **fund's own**. A
group newsroom is not a fund newsroom, and a newsroom on a multi-fund domain
has to be filtered by fund.

What this probe cannot see is the Wix case, which closed the last fund of the
run: a Wix site can hide its own newsroom from the navigation *and* from
``pages-sitemap.xml``. Only ``/sitemap.xml`` -> ``/blog-categories-sitemap.xml``
names it, and ``/blog-posts-sitemap.xml`` enumerates every post with a
``<lastmod>``. Always read the sitemap chain before declaring a site
newsroom-less.

TEMPORARY RECOVERY TOOL - the behaviour belongs in a proper newsroom probe.

Usage::

    uv run python scripts/emergency/probe_page.py <url> [<url> ...]
"""

from __future__ import annotations

import re
import sys

import em

DATE = re.compile(
    r"\b\d{1,2}\.\s?\d{1,2}\.\s?20\d\d|\b20\d\d-\d\d-\d\d|"
    r"\b\d{1,2}\.\s*(ledna|února|března|dubna|května|června|července|srpna|září|"
    r"října|listopadu|prosince)"
)

INTERESTING = re.compile(
    r"aktualit|novink|news|blog|tiskov|media|média|dokument|zprav|report",
    re.I,
)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    for url in sys.argv[1:]:
        meta, body = em.get(url)

        content_type = (meta.get("content_type") or "").lower()

        encoding = "cp1250" if "1250" in content_type else None

        if "pdf" in content_type or body[:5] == b"%PDF-":
            print(f"### PDF {url}")

            continue

        try:
            text = em.html_text(body, encoding)
        except Exception as error:  # noqa: BLE001 - an unreadable page is a result
            print("### ERR", error, url)

            continue

        print(f"\n### [{meta['status']} chars={len(text)} raw={len(body)}] {meta['final_url']}")

        print(text[:600].replace("\n", " | ")[:600])

        try:
            found = em.links(body, encoding=encoding or "utf-8")
        except Exception:  # noqa: BLE001 - link extraction is best effort
            found = []

        seen = set()

        for label, href in found:
            if not href or href.startswith("#"):
                continue

            entry = (label[:60], href)

            if entry in seen:
                continue

            seen.add(entry)

            if DATE.search(label) or INTERESTING.search(f"{label} {href}"):
                print(f"   L: {label[:70]:<70} -> {href[:110]}")
