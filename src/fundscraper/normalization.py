from __future__ import annotations

from urllib.parse import urlparse


def canonical_domain(url: str) -> str:
    """Return a normalized hostname used for grouping funds."""

    hostname = urlparse(url).hostname

    if hostname is None:
        return ""

    return hostname.lower().removeprefix("www.")


def canonical_url(url: str) -> str:
    """Return a lightweight normalized URL used for matching."""

    return url.strip().rstrip("/").lower()
