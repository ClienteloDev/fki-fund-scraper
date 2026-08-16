"""
Split the AVANT hub into one block per fund and per subfond.

``avantfunds.cz/informace-o-fondech/`` is a single server-rendered page of about
4 MB. One ``div.fund-row`` per fund *and* per subfond, each carrying
``data-col-value-for-sorting='<legal name>'`` and its own list of documents.
One fetch therefore maps every AVANT fund to its complete document set, and the
row is the identity evidence - the shared ``avantfunds.cz`` host never is.

227 of the 633 emergency values came through here, the largest single source
family. The richest read inside a row is the annual report's section
``e) Prehled zakladnich financnich a provoznich ukazatelu``, which prints the
current and the prior period side by side: assets_under_management and
aum_history from one table.

CANDIDATE FOR FUTURE ADAPTER - ``src/fundscraper/domain_adapters/avant.py``
already exists; the row splitting and the per-row file list are what it lacks.

Usage::

    uv run python scripts/emergency/avanthub.py <fund name substring>
"""

from __future__ import annotations

import html
import re
import sys

import em

HUB = "https://www.avantfunds.cz/informace-o-fondech/"


def rows():
    """Return (name, subfond names, [(document title, url)], raw block) per fund-row."""

    _meta, body = em.get(HUB)

    page = body.decode("utf-8", "replace")

    out = []

    for block in page.split('<div class="fund-row ')[1:]:
        match = re.search(r"data-col-value-for-sorting='([^']*)'", block)

        if not match:
            continue

        name = html.unescape(match.group(1)).replace("\xa0", " ").strip()

        files = [
            (html.unescape(re.sub(r"\s+", " ", link.group(2))).strip(), link.group(1))
            for link in re.finditer(
                r'<a href="([^"]+\.(?:pdf|xhtml|zip|docx?))"[^>]*>.*?'
                r'class="text-md-regular[^"]*">(.*?)</p>',
                block,
                re.S | re.I,
            )
        ]

        # The podfond blocks nested inside the row. A value read under one of
        # these is SCOPED unless the fund has no other subfond.
        subfonds = [
            html.unescape(name).replace("\xa0", " ").strip()
            for name in re.findall(
                r'class="fund-subrow__name[^"]*"[^>]*>\s*<p[^>]*>(.*?)</p>',
                block,
                re.S,
            )
        ]

        out.append((name, subfonds, files, block))

    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    query = sys.argv[1].lower()

    for name, subfonds, files, _block in rows():
        if query in name.lower():
            print("=== ROW:", name)

            for subfond in subfonds:
                print("    podfond:", subfond)

            for title, url in files:
                print("   ", title, "|", url)
