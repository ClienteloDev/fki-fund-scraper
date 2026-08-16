"""
Find a fund in the public register, and list what it has filed.

Two search rules earned their place:

- Search by the **full legal-name prefix** with ``typHledani=STARTS_WITH``.
  A short prefix walks into the near-miss trap ("TOP ESTATES" matching
  ``Top.Estates Sakura s.r.o.``).
- A fund the prefix search cannot find is found by
  ``typHledani=CONTAINS&jenPlatne=VSECHNY``, which also matches **deleted**
  names. That is the only way to locate a renamed fund, and renames are
  frequent - 6 of the 28 unresolved audit flags are identity changes.

``listing`` returns the (dokument, spis) pairs that ``orsl2.deed_pdf`` needs,
each with the surrounding row text so the filing can be chosen by year and kind
before anything is downloaded. Both calls take the session-bound client from
``orsl2`` - see that module for why there must be only one.

CANDIDATE FOR FUTURE ADAPTER - the search and the listing half of the
``orjustice.py`` adapter.

Usage::

    uv run python scripts/emergency/register_search.py "<legal name>" [--contains] [--all]
"""

from __future__ import annotations

import re
import sys
from urllib.parse import quote

import orsl2

BASE = "https://or.justice.cz/ias/ui/"


def search(c, name, contains=False, all_=False):
    """Return (subjektId, row text) for every hit. ``all_`` includes deleted names."""

    kind = "CONTAINS" if contains else "STARTS_WITH"

    valid = "VSECHNY" if all_ else "PLATNE"

    response = c.get(
        f"{BASE}rejstrik-$firma?nazev={quote(name)}&jenPlatne={valid}&polozek=50&typHledani={kind}"
    )

    results = []

    seen = set()

    for block in re.split(r"<li ", response.text):
        match = re.search(r"vysledky\?subjektId=(\d+)", block)

        if not match:
            continue

        subject_id = match.group(1)

        if subject_id in seen:
            continue

        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()

        text = text.replace("Výpis platných", "").replace("Úplný výpis", "")

        seen.add(subject_id)

        results.append((subject_id, text[:150]))

    return results


def listing(c, subject_id):
    """Return (dokument, spis, row text) for every filing in the firm's Sbirka listin."""

    response = c.get(f"{BASE}vypis-sl-firma?subjektId={subject_id}")

    rows = []

    for part in re.split(r"<tr", response.text):
        match = re.search(
            r"vypis-sl-detail\?dokument=(\d+)&amp;subjektId=\d+&amp;spis=(\d+)",
            part,
        )

        if not match:
            continue

        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", part)).strip()

        rows.append((match.group(1), match.group(2), text[:260]))

    return rows


def full_extract_url(subject_id):
    """
    The uplny vypis, which carries historic names, the board and ``v likvidaci``.

    For a SICAV under s. 95(1)(a) ZISIF the sole board member is the
    obhospodarovatel, so this page alone can settle ``manager``.
    """

    return f"{BASE}rejstrik-firma.vysledky?subjektId={subject_id}&typ=UPLNY"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    query = sys.argv[1]

    with orsl2.client() as session:
        hits = search(
            session,
            query,
            contains="--contains" in sys.argv,
            all_="--all" in sys.argv,
        )

        for subject_id, name in hits:
            print(subject_id, name)

        if len(hits) == 1:
            print()

            for dokument, spis, text in listing(session, hits[0][0]):
                print(f"  dokument={dokument} spis={spis} | {text}")
