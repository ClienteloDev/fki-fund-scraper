from __future__ import annotations

from fundscraper.normalization import (
    canonical_domain,
    canonical_url,
)


def test_canonical_domain_removes_www() -> None:
    assert canonical_domain("https://www.Example.COM/documents") == "example.com"


def test_canonical_url_normalizes_host_and_fragment() -> None:
    result = canonical_url("HTTPS://Example.COM:443/Documents/KID.pdf#page=3")

    assert result == "https://example.com/Documents/KID.pdf"


def test_canonical_url_preserves_path_case() -> None:
    uppercase_path = canonical_url("https://example.com/Documents/KID.pdf")

    lowercase_path = canonical_url("https://example.com/documents/kid.pdf")

    assert uppercase_path != lowercase_path


def test_canonical_url_normalizes_trailing_slash() -> None:
    first = canonical_url("https://example.com/documents/")

    second = canonical_url("https://example.com/documents")

    assert first == second
