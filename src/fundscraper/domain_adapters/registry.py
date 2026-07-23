from __future__ import annotations

from fundscraper.domain_adapters.avant import (
    AvantFundsAdapter,
)
from fundscraper.domain_adapters.base import (
    DomainAdapter,
)
from fundscraper.models import FundInput

ADAPTERS: tuple[DomainAdapter, ...] = (AvantFundsAdapter(),)


def get_domain_adapter(
    fund: FundInput,
) -> DomainAdapter | None:
    """Return the first adapter supporting the fund."""

    for adapter in ADAPTERS:
        if adapter.supports(fund):
            return adapter

    return None
