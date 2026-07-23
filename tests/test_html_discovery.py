from __future__ import annotations

from fundscraper.html_discovery import (
    classify_link,
    is_html_response,
    parse_html_page,
    resolve_link_url,
)
from fundscraper.output_models import DocumentType


def test_parses_and_ranks_document_links() -> None:
    html = b"""
    <!doctype html>
    <html>
      <head>
        <title>Example Fund Documents</title>
        <link
          rel="canonical"
          href="https://example.com/fund"
        >
      </head>
      <body>
        <a href="/documents/kid.pdf">
          Sdeleni klicovych informaci
        </a>

        <a href="/documents/annual-report-2025.pdf">
          Vyrocni zprava 2025
        </a>

        <a href="/documents/annual-report-2025.pdf">
          Stahnout PDF
        </a>

        <a href="mailto:info@example.com">
          Contact
        </a>

        <a href="#fees">
          Fees
        </a>
      </body>
    </html>
    """

    page = parse_html_page(
        body=html,
        page_url="https://example.com/fund/",
    )

    assert page.title == "Example Fund Documents"

    assert page.canonical_page_url == "https://example.com/fund"

    assert page.links_total == 5
    assert len(page.candidates) == 2

    assert page.candidates[0].document_type is DocumentType.PRIIPS_KID

    assert page.candidates[0].url == "https://example.com/documents/kid.pdf"

    assert page.candidates[1].document_type is DocumentType.ANNUAL_REPORT


def test_resolves_relative_url_and_removes_fragment() -> None:
    result = resolve_link_url(
        base_url="https://example.com/funds/example/",
        raw_href="../documents/statut.pdf#page=2",
    )

    assert result == "https://example.com/funds/documents/statut.pdf"


def test_skips_non_http_links() -> None:
    assert (
        resolve_link_url(
            base_url="https://example.com",
            raw_href="mailto:info@example.com",
        )
        is None
    )

    assert (
        resolve_link_url(
            base_url="https://example.com",
            raw_href="javascript:void(0)",
        )
        is None
    )


def test_classifies_target_document() -> None:
    candidate = classify_link(
        url=("https://example.com/documents/investicni-memorandum.pdf"),
        text="Investicni memorandum",
        page_url="https://example.com",
    )

    assert candidate is not None
    assert candidate.document_type is DocumentType.MEMORANDUM
    assert candidate.direct_document is True
    assert candidate.same_domain is True
    assert candidate.score >= 90


def test_ignores_irrelevant_link() -> None:
    candidate = classify_link(
        url="https://example.com/contact",
        text="Kontakt",
        page_url="https://example.com",
    )

    assert candidate is None


def test_detects_html_without_content_type() -> None:
    assert is_html_response(
        content_type=None,
        body=b"<!doctype html><html></html>",
    )
