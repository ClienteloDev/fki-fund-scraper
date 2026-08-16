"""
Session-bound access to the Sbirka listin of the public register.

The single defect that cost stage 12 its whole yield: ``vypis-sl-detail`` links
are **session-bound**. Fetched with a fresh client they return
"Neplatny odkaz" and the helper silently returns ``None`` for every document.
The listing (``vypis-sl-firma``), the detail and the ``/ias/content/download``
call must share one ``httpx.Client``. Fixing that made the register the
second-largest source family of the emergency work - 118 values.

The register is worth the trouble for two reasons beyond the deeds themselves:
its filing often contains the **subfund** report the administrator's panel
omits, so a shell SICAV still yields a fund-level figure; and for a SICAV under
s. 95(1)(a) ZISIF the sole member of the board **is** the obhospodarovatel
(s. 9(1) ZISIF), so the uplny vypis alone yields ``manager`` even when every
filed deed is an image-only scan.

Downloaded deeds are written into the ``em`` cache chain, so a deed is fetched
once across every stage.

CANDIDATE FOR FUTURE ADAPTER - ``orjustice.py`` is the single biggest missing
capability named in the pre-refactor handoff.

Usage::

    uv run python scripts/emergency/orsl2.py <subjektId> <dokument> <spis>
"""

from __future__ import annotations

import json
import re
import sys

import em
import httpx

BASE = "https://or.justice.cz/ias/ui/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def client():
    """One client for a whole register conversation. Never make a second one."""

    return httpx.Client(
        follow_redirects=True,
        timeout=60,
        verify=False,  # noqa: S501 - recovery tool; see em.py
        headers={"User-Agent": UA, "Accept-Language": "cs,en;q=0.8"},
    )


def deed_pdf(c, subject_id, dokument, spis):
    """
    Walk listing -> detail -> download inside one session and return (url, body).

    The first call seats the session on the firm's listing. Skipping it is what
    makes the detail return "Neplatny odkaz".
    """

    c.get(f"{BASE}vypis-sl-firma?subjektId={subject_id}")

    detail = c.get(f"{BASE}vypis-sl-detail?dokument={dokument}&subjektId={subject_id}&spis={spis}")

    match = re.search(r'href="(/ias/content/download\?id=[0-9a-f]+)"', detail.text)

    if not match:
        return None, None

    url = "https://or.justice.cz" + match.group(1)

    hit = em.cached(url)

    if hit:
        return url, hit[1]

    response = c.get(url)

    em.OUT.mkdir(parents=True, exist_ok=True)

    k = em.key(url)

    (em.OUT / f"{k}.meta.json").write_text(
        json.dumps(
            {
                "url": url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "final_url": str(response.url),
                "len": len(response.content),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (em.OUT / f"{k}.body").write_bytes(response.content)

    return url, response.content


def spis_of(c, subject_id):
    """Return the spis number the firm's listing links its documents under."""

    listing = c.get(f"{BASE}vypis-sl-firma?subjektId={subject_id}")

    match = re.search(
        r"vypis-sl-detail\?dokument=\d+&amp;subjektId=\d+&amp;spis=(\d+)",
        listing.text,
    )

    return match.group(1) if match else None


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    subject_id, dokument, spis = sys.argv[1], sys.argv[2], sys.argv[3]

    with client() as session:
        url, body = deed_pdf(session, subject_id, dokument, spis)

        print(url, len(body) if body else None)
