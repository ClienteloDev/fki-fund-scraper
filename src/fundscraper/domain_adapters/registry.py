from __future__ import annotations

from fundscraper.domain_adapters.avant import (
    AvantFundsAdapter,
)
from fundscraper.domain_adapters.base import (
    DomainAdapter,
)
from fundscraper.domain_adapters.porovnejfondy import (
    PorovnejFondyAdapter,
)
from fundscraper.models import FundInput

AVANT_ADAPTER = AvantFundsAdapter()
POROVNEJ_FONDY_ADAPTER = PorovnejFondyAdapter()

ADAPTERS: tuple[DomainAdapter, ...] = (
    AVANT_ADAPTER,
    POROVNEJ_FONDY_ADAPTER,
)


def get_domain_adapter(
    fund: FundInput,
) -> DomainAdapter | None:
    """Return the first adapter explicitly supporting the fund."""

    for adapter in ADAPTERS:
        if adapter.supports(fund):
            return adapter

    return None


def get_avant_fallback_adapter() -> DomainAdapter:
    """Return AVANT as an explicit fallback document provider."""

    return AVANT_ADAPTER


def get_porovnejfondy_fallback_adapter() -> DomainAdapter:
    """Return PorovnejFondy.cz as an explicit fallback page provider."""

    return POROVNEJ_FONDY_ADAPTER
