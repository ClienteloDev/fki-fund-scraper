from __future__ import annotations

from fundscraper.domain_adapters.bhs import (
    BhsAdapter,
    match_bhs_fund,
    parse_bhs_catalog,
)
from fundscraper.models import FundInput


def test_matches_bhs_catalog_entry() -> None:
    body = b"""
    <html><body>
      <a href="/dynamicky-fond">BHS DYNAMIC FUND SICAV, a.s.</a>
      <a href="/nemovitostni-fond">BHS REAL ESTATE FUND SICAV, a.s.</a>
    </body></html>
    """

    entries = parse_bhs_catalog(
        body=body,
        page_url="https://www.bhs.cz/fondy",
    )
    match = match_bhs_fund(
        entries=entries,
        fund_name="BHS DYNAMIC FUND SICAV, a.s.",
    )

    assert match is not None
    assert match.url == "https://www.bhs.cz/dynamicky-fond"


def test_bhs_adapter_supports_bhs_domain() -> None:
    adapter = BhsAdapter()
    fund = FundInput(
        name="BHS DYNAMIC FUND SICAV, a.s.",
        web="https://www.bhs.cz/fondy",
    )

    assert adapter.supports(fund) is True
