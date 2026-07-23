from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def canonical_domain(url: str) -> str:
    """Return a normalized hostname used for grouping funds."""

    parsed = urlsplit(url.strip())
    hostname = parsed.hostname

    if hostname is None:
        return ""

    return hostname.lower().removeprefix("www.")


def canonical_url(url: str) -> str:
    """
    Normalize a URL for matching and duplicate detection.

    Scheme and hostname are case-insensitive. Path and query values are
    preserved because they may be case-sensitive.
    """

    parsed = urlsplit(url.strip())

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname

    if hostname is None:
        return url.strip().rstrip("#")

    normalized_hostname = hostname.lower()

    if ":" in normalized_hostname and not normalized_hostname.startswith("["):
        normalized_hostname = f"[{normalized_hostname}]"

    port = parsed.port

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)

    if port is not None and not default_port:
        netloc = f"{normalized_hostname}:{port}"
    else:
        netloc = normalized_hostname

    path = parsed.path or "/"

    if path != "/":
        path = path.rstrip("/")

    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            parsed.query,
            "",
        )
    )
