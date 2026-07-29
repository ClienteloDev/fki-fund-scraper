from __future__ import annotations

from fundscraper.domain_adapters.registry import (
    get_avant_fallback_adapter,
    get_domain_adapter,
)
from fundscraper.models import FundInput


def test_returns_avant_adapter() -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://www.avantfunds.cz/fondy/example/",
    )

    adapter = get_domain_adapter(fund)

    assert adapter is not None
    assert adapter.name == "avantfunds"


def test_returns_none_for_unknown_domain() -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com",
    )

    assert get_domain_adapter(fund) is None


def test_returns_avant_adapter_for_spilberk() -> None:
    fund = FundInput(
        name=("SPILBERK investiční fond SICAV, a.s."),
        web="https://www.spilberk.com/",
    )

    adapter = get_domain_adapter(fund)

    assert adapter is not None
    assert adapter.name == "avantfunds"


def test_returns_avant_adapter_for_nemomax() -> None:
    fund = FundInput(
        name=("Nemomax investiční fond s proměnným základním kapitálem, a.s."),
        web="https://nemomax.cz/",
    )

    adapter = get_domain_adapter(fund)

    assert adapter is not None
    assert adapter.name == "avantfunds"


def test_returns_avant_fallback_adapter_for_unknown_domain() -> None:
    adapter = get_avant_fallback_adapter()

    assert adapter.name == "avantfunds"
