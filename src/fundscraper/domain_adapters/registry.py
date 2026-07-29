from __future__ import annotations

from fundscraper.domain_adapters.amista import AmistaAdapter
from fundscraper.domain_adapters.avant import AvantFundsAdapter
from fundscraper.domain_adapters.base import DomainAdapter
from fundscraper.domain_adapters.bhs import BhsAdapter
from fundscraper.domain_adapters.porovnejfondy import PorovnejFondyAdapter
from fundscraper.models import FundInput

AMISTA_ADAPTER = AmistaAdapter()
AVANT_ADAPTER = AvantFundsAdapter()
BHS_ADAPTER = BhsAdapter()
POROVNEJ_FONDY_ADAPTER = PorovnejFondyAdapter()

ADAPTERS: tuple[DomainAdapter, ...] = (
    BHS_ADAPTER,
    AMISTA_ADAPTER,
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


def get_amista_fallback_adapter() -> DomainAdapter:
    """Return AMISTA as a cross-domain fund-profile provider."""

    return AMISTA_ADAPTER


def get_avant_fallback_adapter() -> DomainAdapter:
    """Return AVANT as an explicit fallback document provider."""

    return AVANT_ADAPTER


def get_porovnejfondy_fallback_adapter() -> DomainAdapter:
    """Return PorovnejFondy.cz as an explicit fallback page provider."""

    return POROVNEJ_FONDY_ADAPTER
