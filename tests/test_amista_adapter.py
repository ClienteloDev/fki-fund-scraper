from __future__ import annotations

from fundscraper.domain_adapters.amista import (
    AmistaAdapter,
    match_amista_fund,
    parse_amista_catalog,
)
from fundscraper.models import FundInput


def test_matches_amista_catalog_entry_with_podfond_suffix() -> None:
    body = b"""
    <html><body>
      <a href="/fondefi-sicav-as-realitni-podfond.html">
        FONDEFI SICAV, a.s., realitni podfond
      </a>
      <a href="/tmr-fond-sicav-as.html">TMR Fond SICAV a.s.</a>
    </body></html>
    """

    entries = parse_amista_catalog(
        body=body,
        page_url="https://www.amista.cz/investovani.html",
    )
    match = match_amista_fund(
        entries=entries,
        fund_name="FONDEFI SICAV, a.s.",
    )

    assert match is not None
    assert match.url == "https://www.amista.cz/fondefi-sicav-as-realitni-podfond.html"


def test_amista_rejects_unrelated_single_token_match() -> None:
    body = b"""
    <html><body>
      <a href="/new-europe-sicav.html">NEW EUROPE SICAV a.s.</a>
    </body></html>
    """

    entries = parse_amista_catalog(
        body=body,
        page_url="https://www.amista.cz/investovani.html",
    )

    assert (
        match_amista_fund(
            entries=entries,
            fund_name="NEW SICAV a.s.",
        )
        is None
    )


def test_amista_adapter_supports_amista_domain() -> None:
    adapter = AmistaAdapter()
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://www.amista.cz/example.html",
    )

    assert adapter.supports(fund) is True
