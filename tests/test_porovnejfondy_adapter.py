from __future__ import annotations

from fundscraper.domain_adapters.porovnejfondy import (
    match_porovnejfondy_fund,
    parse_porovnejfondy_catalog,
)


def test_matches_exact_fund_catalog_entry() -> None:
    body = b"""
    <html>
      <body>
        <a href="/fond/bohemia-investicni-fond-cz0000000001/">
          BOHEMIA investicni fond SICAV, a.s.
        </a>
        <a href="/fond/creditas-nemovitostni-cz0000000002/">
          CREDITAS Nemovitostni I
        </a>
      </body>
    </html>
    """

    entries = parse_porovnejfondy_catalog(
        body=body,
        page_url="https://www.porovnejfondy.cz/fond/",
    )

    match = match_porovnejfondy_fund(
        entries=entries,
        fund_name="BOHEMIA investicni fond SICAV, a.s.",
    )

    assert match is not None
    assert match.url == ("https://www.porovnejfondy.cz/fond/bohemia-investicni-fond-cz0000000001/")


def test_rejects_insufficient_creditas_name_match() -> None:
    body = b"""
    <html>
      <body>
        <a href="/fond/creditas-nemovitostni-cz0000000002/">
          CREDITAS Nemovitostni I
        </a>
      </body>
    </html>
    """

    entries = parse_porovnejfondy_catalog(
        body=body,
        page_url="https://www.porovnejfondy.cz/fond/",
    )

    match = match_porovnejfondy_fund(
        entries=entries,
        fund_name="CREDITAS ASSETS SICAV a.s.",
    )

    assert match is None


def test_rejects_unrelated_single_token_match() -> None:
    body = b"""
    <html>
      <body>
        <a href="/fond/careful-global-equity-cz0000000003/">
          Careful Global Equity
        </a>
      </body>
    </html>
    """

    entries = parse_porovnejfondy_catalog(
        body=body,
        page_url="https://www.porovnejfondy.cz/fond/",
    )

    match = match_porovnejfondy_fund(
        entries=entries,
        fund_name="CARE SICAV, a.s.",
    )

    assert match is None
