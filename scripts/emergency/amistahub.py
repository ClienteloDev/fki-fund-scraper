"""
Split the AMISTA hub into one accordion panel per fund.

Two things make this hub work, and both were discovered the hard way:

1. **The page is cp1250, not utf-8.** Decoded as utf-8 every Czech fund name
   silently fails to match and all 103 panels look empty. One decoding flag
   unlocked 85 values.
2. **The panel is the identity evidence.** One ``div.m-toggle.jq_toggle`` per
   fund, with the fund's own name in ``span.m-toggle__title``. The shared
   ``amista.cz`` host proves nothing on its own. ``download.php?id=`` redirects
   into a fund-specific folder (``/files/outuln/``, ``/files/ceesic/``, ...),
   which is a second, independent identity signal - follow the redirect and
   read the folder.

CANDIDATE FOR FUTURE ADAPTER - ``src/fundscraper/domain_adapters/amista.py``
already exists; the cp1250 decode and the panel split are what it lacks.

Usage::

    uv run python scripts/emergency/amistahub.py <fund name substring>
"""

from __future__ import annotations

import html
import re
import sys

import em

HUB = "https://www.amista.cz/povinne-informace.html"


def page():
    """Return the hub HTML, decoded as cp1250."""

    _meta, body = em.get(HUB)

    return body.decode("cp1250", "replace")


def panels(text: str):
    """Return (fund name, the fund's own panel block) for every panel on the hub."""

    starts = [match.start() for match in re.finditer(r'<div class="m-toggle jq_toggle"', text)]

    out = []

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)

        block = text[start:end]

        title = re.search(r'(?is)<span class="m-toggle__title">(.*?)</span>', block)

        if not title:
            continue

        out.append(
            (
                html.unescape(re.sub(r"<[^>]+>|\s+", " ", title.group(1))).strip(),
                block,
            )
        )

    return out


def docs(block: str):
    """Return (document title, absolute download url) for one panel."""

    out = []

    for match in re.finditer(
        r'(?is)<a[^>]*href="([^"]*download\.php\?id=\d+[^"]*)"[^>]*>(.*?)</a>',
        block,
    ):
        title = html.unescape(re.sub(r"<[^>]+>|\s+", " ", match.group(2))).strip()

        url = html.unescape(match.group(1))

        if not url.startswith("http"):
            url = "https://www.amista.cz/" + url.lstrip("/")

        out.append((title, url))

    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    hub = page()

    print("hub chars:", len(hub), file=sys.stderr)

    query = sys.argv[1].lower()

    for name, block in panels(hub):
        if query in name.lower():
            print("=== PANEL:", name)

            for title, url in docs(block):
                print("   ", title, "|", url)
