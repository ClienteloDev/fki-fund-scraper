from __future__ import annotations

from fundscraper.site_crawler import (
    discover_navigation_links,
    fund_name_tokens,
)


def test_extracts_significant_fund_tokens() -> None:
    tokens = fund_name_tokens("AVANT Finance SICAV a. s.")

    assert "avant" in tokens
    assert "finance" in tokens
    assert "sicav" not in tokens


def test_discovers_fund_and_document_pages() -> None:
    html = b"""
    <html>
      <body>
        <a href="/fondy/avant-finance">
          AVANT Finance
        </a>

        <a href="/pro-investory">
          Pro investory
        </a>

        <a href="/kontakt">
          Kontakt
        </a>

        <a href="/privacy">
          Privacy
        </a>

        <a href="/documents/statut.pdf">
          Statut
        </a>
      </body>
    </html>
    """

    links = discover_navigation_links(
        body=html,
        page_url="https://example.com/",
        effective_base_url="https://example.com/",
        fund_name="AVANT Finance SICAV a. s.",
    )

    urls = {link.url for link in links}

    assert "https://example.com/fondy/avant-finance" in urls

    assert "https://example.com/pro-investory" in urls

    assert "https://example.com/kontakt" not in urls

    assert "https://example.com/privacy" not in urls

    assert "https://example.com/documents/statut.pdf" not in urls
