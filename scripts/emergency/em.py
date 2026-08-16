"""
The fetch and read layer the emergency enrichment ran on.

One cache entry per URL, keyed by a truncated digest of the URL, stored as a
``.meta.json`` / ``.body`` pair. Reads walk the whole chain of emergency caches
newest first, so a document any stage already fetched is never fetched again;
writes go to the newest cache only. The chain is what makes
``cache/emergency-*`` the regression corpus the adapters should be built
against.

``html_text`` tries cp1250 before latin-1 because the AMISTA hub is served in
cp1250 and decoding it as utf-8 silently loses every Czech fund name.

RECOVERY TOOL, not a production component. In particular it disables TLS
verification, keeps no rate limiting and pretends to be a desktop browser -
three things the production ``http_client`` does properly and this must never
teach it.

Usage::

    uv run python scripts/emergency/em.py <url> [grep pattern ...]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import httpx
from selectolax.parser import HTMLParser

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# Newest first: a read stops at the first cache that holds the URL, and every
# write lands in CACHES[0].
CACHES = [
    REPOSITORY_ROOT / "cache" / name
    for name in (
        "emergency-stage-17",
        "emergency-stage-16",
        "emergency-stage-15",
        "emergency-stage-14",
        "emergency-stage-13",
        "emergency-stage-12",
        "emergency-stage-11",
        "emergency-stage-10",
        "emergency-stage-09",
        "emergency-stage-08",
        "emergency-stage-07",
        "emergency-stage-06",
        "emergency-stage-05",
        "emergency-stage-04",
        "emergency-stage-03",
        "emergency-stage-02",
        "emergency-worker-1",
    )
]

OUT = CACHES[0]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def key(url):
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def cached(url):
    """Return (meta, body) from the newest cache that holds the URL, else None."""

    k = key(url)

    for cache in CACHES:
        meta_path = cache / f"{k}.meta.json"

        if meta_path.exists():
            return (
                json.loads(meta_path.read_text(encoding="utf-8")),
                (cache / f"{k}.body").read_bytes(),
            )

    return None


def get(url, force=False, timeout=45):
    """Return (meta, body), fetching only when the chain does not hold the URL."""

    hit = None if force else cached(url)

    if hit:
        return hit

    OUT.mkdir(parents=True, exist_ok=True)

    k = key(url)

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": UA, "Accept-Language": "cs,en;q=0.8"},
            verify=False,  # noqa: S501 - recovery tool; see the module docstring
        ) as client:
            response = client.get(url)

            body = response.content

            meta = {
                "url": url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "final_url": str(response.url),
                "len": len(body),
            }
    except Exception as error:  # noqa: BLE001 - a failed fetch is cached as a failure
        meta = {
            "url": url,
            "status": 0,
            "error": f"{type(error).__name__}: {error}",
            "content_type": "",
            "final_url": url,
            "len": 0,
        }

        body = b""

    (OUT / f"{k}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    (OUT / f"{k}.body").write_bytes(body)

    return meta, body


def pdf_text(body, maxpages=None):
    """Return (text, page_count). Probe the length before trusting the text."""

    import fitz

    document = fitz.open(stream=body, filetype="pdf")

    pages = document.page_count if maxpages is None else min(document.page_count, maxpages)

    return "\n".join(document.load_page(i).get_text() for i in range(pages)), document.page_count


def html_text(body, encoding=None):
    """Return the visible text of an HTML body, cp1250-aware."""

    if encoding:
        text = body.decode(encoding, "replace")
    else:
        text = None

        for candidate in ("utf-8", "cp1250", "latin-1"):
            try:
                text = body.decode(candidate)

                break
            except UnicodeDecodeError:
                continue

        if text is None:
            text = body.decode("utf-8", "replace")

    tree = HTMLParser(text)

    for tag in tree.css("script,style,noscript,svg"):
        tag.decompose()

    visible = tree.body.text(separator="\n") if tree.body else tree.text(separator="\n")

    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", visible)).strip()


def links(body, base="", encoding=None):
    """Return every (anchor text, href) pair of an HTML body."""

    tree = HTMLParser(body.decode(encoding or "utf-8", "replace"))

    return [(a.text(strip=True), a.attributes.get("href") or "") for a in tree.css("a")]


def probe(url, force=False):
    """
    Return (meta, text, page_count) for a URL of either kind.

    The page count is what tells an image-only scan or an ESEF package from a
    readable filing: under ~500 characters over 10+ pages there is no text
    layer and the previous year has to be tried instead.
    """

    meta, body = get(url, force=force)

    content_type = (meta.get("content_type") or "").lower()

    if "pdf" in content_type or body[:5] == b"%PDF-":
        try:
            text, pages = pdf_text(body)
        except Exception as error:  # noqa: BLE001 - a broken PDF is a result, not a crash
            return meta, f"[PDF ERROR {error}]", 0

        return meta, text, pages

    return meta, html_text(body), 0


def show(url, grep=None, ctx=3, limit=6000, force=False, encoding=None):
    """Print a document, or only the lines around each pattern that matches."""

    meta, body = get(url, force=force)

    content_type = (meta.get("content_type") or "").lower()

    if "pdf" in content_type or body[:5] == b"%PDF-":
        text, pages = pdf_text(body)

        print(f"[PDF {meta['status']} pages={pages} chars={len(text)}] {meta['final_url']}")
    else:
        text = html_text(body, encoding)

        print(f"[HTML {meta['status']} chars={len(text)}] {meta['final_url']}")

    if not grep:
        print(text[:limit])

        return

    patterns = grep if isinstance(grep, list | tuple) else [grep]

    lines = text.split("\n")

    hits: set[int] = set()

    for index, line in enumerate(lines):
        for pattern in patterns:
            if re.search(pattern, line, re.I):
                hits.update(range(max(0, index - ctx), min(len(lines), index + ctx + 1)))

    previous = -2

    for index in sorted(hits):
        if index != previous + 1:
            print("  ...")

        print(f"{index:>5}| {lines[index]}")

        previous = index


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    show(sys.argv[1], grep=sys.argv[2:] or None)
