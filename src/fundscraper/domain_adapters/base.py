from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fundscraper.html_discovery import DiscoveredLink
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput


@dataclass(frozen=True, slots=True)
class DomainAdapterResult:
    """URLs and documents discovered by one domain adapter."""

    adapter_name: str
    navigation_urls: tuple[str, ...]
    documents: tuple[DiscoveredLink, ...]
    warnings: tuple[str, ...]


class DomainAdapter(Protocol):
    """Interface implemented by domain-specific discovery adapters."""

    name: str

    def supports(
        self,
        fund: FundInput,
    ) -> bool:
        """Return whether the adapter supports the fund website."""

    async def discover(
        self,
        *,
        fund: FundInput,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> DomainAdapterResult:
        """Discover fund-specific pages and documents."""
