from __future__ import annotations

from fundscraper.domain_adapters.registry import (
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
