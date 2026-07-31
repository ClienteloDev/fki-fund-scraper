from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from selectolax.lexbor import LexborHTMLParser

from fundscraper.domain_adapters.base import DomainAdapterResult
from fundscraper.html_discovery import normalize_search_text, resolve_link_url
from fundscraper.http_client import FetchError, HttpFetcher
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain, canonical_url

BHS_DOMAIN = "bhs.cz"
BHS_CATALOG_URL = "https://www.bhs.cz/fondy"

BHS_DIRECT_FUND_URLS = {
    "bhs dynamic": "https://www.bhs.cz/dynamicky-fond",
    "bhs energy battery": "https://www.bhs.cz/energeticky-a-bateriovy-fond",
    "bhs real estate": "https://www.bhs.cz/nemovitostni-fond",
    "bhs iconic cars": "https://www.bhs.cz/fond-ikonickych-automobilu",
}

GENERIC_FUND_TOKENS = frozenset(
    {
        "a",
        "as",
        "fond",
        "fund",
        "fonds",
        "investicni",
        "investment",
        "kapital",
        "kapitalem",
        "podfond",
        "promenneho",
        "promennym",
        "promenny",
        "s",
        "sicav",
        "spolecnost",
        "zakladniho",
        "zakladnim",
    }
)


@dataclass(frozen=True, slots=True)
class BhsCatalogEntry:
    url: str
    text: str
    key: str
    tokens: frozenset[str]


class BhsAdapter:
    """Resolve BHS fund profile pages containing structured investment data."""

    name = "bhs"

    def __init__(self) -> None:
        self._cached_entries: tuple[BhsCatalogEntry, ...] | None = None

    def supports(self, fund: FundInput) -> bool:
        return canonical_domain(fund.web) == BHS_DOMAIN

    async def discover(
        self,
        *,
        fund: FundInput,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> DomainAdapterResult:
        target_key = _entity_key(fund.name)

        direct_url = BHS_DIRECT_FUND_URLS.get(target_key)

        if direct_url is not None:
            return DomainAdapterResult(
                adapter_name=self.name,
                navigation_urls=(direct_url,),
                documents=(),
                warnings=(),
            )

        entries = self._cached_entries

        if entries is None or force:
            try:
                result = await fetcher.fetch(
                    BHS_CATALOG_URL,
                    force=force,
                )
            except FetchError as exc:
                return DomainAdapterResult(
                    adapter_name=self.name,
                    navigation_urls=(),
                    documents=(),
                    warnings=(f"BHS catalog could not be downloaded: {exc.code}: {exc}",),
                )

            entries = parse_bhs_catalog(
                body=result.body,
                page_url=result.final_url,
            )

            if not force:
                self._cached_entries = entries

        matched_entry = match_bhs_fund(
            entries=entries,
            fund_name=fund.name,
        )

        if matched_entry is None:
            return DomainAdapterResult(
                adapter_name=self.name,
                navigation_urls=(),
                documents=(),
                warnings=(
                    f"No sufficiently exact BHS catalog match was found for fund: {fund.name}",
                ),
            )

        return DomainAdapterResult(
            adapter_name=self.name,
            navigation_urls=(matched_entry.url,),
            documents=(),
            warnings=(),
        )


def parse_bhs_catalog(
    *,
    body: bytes,
    page_url: str,
) -> tuple[BhsCatalogEntry, ...]:
    if not body:
        return ()

    parser = LexborHTMLParser(body)
    entries_by_url: dict[str, BhsCatalogEntry] = {}

    for node in parser.css("a[href]"):
        raw_href = node.attributes.get("href")

        if not raw_href:
            continue

        resolved_url = resolve_link_url(
            base_url=page_url,
            raw_href=raw_href,
        )

        if resolved_url is None or canonical_domain(resolved_url) != BHS_DOMAIN:
            continue

        normalized_url = canonical_url(resolved_url)

        if normalized_url.rstrip("/") == BHS_CATALOG_URL.rstrip("/"):
            continue

        text = _node_text(node)
        key = _entity_key(text)

        if not key:
            continue

        candidate = BhsCatalogEntry(
            url=resolved_url,
            text=text,
            key=key,
            tokens=frozenset(key.split()),
        )

        current = entries_by_url.get(normalized_url)

        if current is None or len(candidate.text) > len(current.text):
            entries_by_url[normalized_url] = candidate

    return tuple(sorted(entries_by_url.values(), key=lambda item: item.url))


def match_bhs_fund(
    *,
    entries: tuple[BhsCatalogEntry, ...],
    fund_name: str,
) -> BhsCatalogEntry | None:
    target_key = _entity_key(fund_name)
    target_tokens = frozenset(target_key.split())

    if not target_key or not target_tokens:
        return None

    best_entry: BhsCatalogEntry | None = None
    best_score = 0.0

    for entry in entries:
        score = _match_score(
            target_key=target_key,
            target_tokens=target_tokens,
            candidate_key=entry.key,
            candidate_tokens=entry.tokens,
        )

        if score > best_score:
            best_entry = entry
            best_score = score

    if best_entry is None or best_score < 0.74:
        return None

    return best_entry


def _match_score(
    *,
    target_key: str,
    target_tokens: frozenset[str],
    candidate_key: str,
    candidate_tokens: frozenset[str],
) -> float:
    if target_key == candidate_key:
        return 1.0

    overlap = target_tokens & candidate_tokens

    if not overlap:
        return 0.0

    target_coverage = len(overlap) / len(target_tokens)
    candidate_coverage = len(overlap) / len(candidate_tokens)

    if target_coverage < 0.66:
        return 0.0

    sequence_ratio = SequenceMatcher(None, target_key, candidate_key).ratio()
    substring_bonus = 0.12 if target_key in candidate_key else 0.0

    return min(
        1.0,
        (target_coverage * 0.58)
        + (candidate_coverage * 0.17)
        + (sequence_ratio * 0.25)
        + substring_bonus,
    )


def _entity_key(value: str) -> str:
    normalized = normalize_search_text(value)
    tokens = re.findall(r"[a-z0-9]+", normalized)
    result: list[str] = []

    for token in tokens:
        if token in GENERIC_FUND_TOKENS or len(token) < 2 or token.isdigit():
            continue

        if token not in result:
            result.append(token)

    return " ".join(result)


def _node_text(node: object) -> str:
    text_method = getattr(node, "text", None)

    if not callable(text_method):
        return ""

    text = text_method(
        separator=" ",
        strip=True,
    )

    return re.sub(r"\s+", " ", str(text)).strip()
