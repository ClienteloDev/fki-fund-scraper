from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from selectolax.lexbor import LexborHTMLParser

from fundscraper.domain_adapters.base import DomainAdapterResult
from fundscraper.html_discovery import (
    decode_html_bytes,
    normalize_search_text,
    resolve_link_url,
)
from fundscraper.http_client import FetchError, HttpFetcher
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain, canonical_url

AMISTA_DOMAIN = "amista.cz"

# The mandatory information page lists every administered fund together
# with its documents, including funds that have no own detail page.
AMISTA_CATALOG_URL = "https://www.amista.cz/povinne-informace.html"

GENERIC_FUND_TOKENS = frozenset(
    {
        "a",
        "as",
        "czk",
        "eur",
        "fond",
        "fund",
        "fonds",
        "investicni",
        "investment",
        "kapital",
        "kapitalem",
        "nemovitostni",
        "podfond",
        "promenneho",
        "promennym",
        "promenny",
        "realitni",
        "s",
        "sicav",
        "spolecnost",
        "zakladniho",
        "zakladnim",
    }
)


@dataclass(frozen=True, slots=True)
class AmistaCatalogEntry:
    url: str
    text: str
    key: str
    tokens: frozenset[str]


class AmistaAdapter:
    """Resolve exact AMISTA fund profiles for direct and cross-domain use."""

    name = "amista"

    def __init__(self) -> None:
        self._cached_entries: tuple[AmistaCatalogEntry, ...] | None = None

    def supports(self, fund: FundInput) -> bool:
        return canonical_domain(fund.web) == AMISTA_DOMAIN

    async def discover(
        self,
        *,
        fund: FundInput,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> DomainAdapterResult:
        entries = self._cached_entries

        if entries is None or force:
            try:
                result = await fetcher.fetch(
                    AMISTA_CATALOG_URL,
                    force=force,
                )
            except FetchError as exc:
                return DomainAdapterResult(
                    adapter_name=self.name,
                    navigation_urls=(),
                    documents=(),
                    warnings=(f"AMISTA catalog could not be downloaded: {exc.code}: {exc}",),
                )

            entries = parse_amista_catalog(
                body=result.body,
                page_url=result.final_url,
            )

            if not force:
                self._cached_entries = entries

        matched_entry = match_amista_fund(
            entries=entries,
            fund_name=fund.name,
        )

        if matched_entry is None:
            return DomainAdapterResult(
                adapter_name=self.name,
                navigation_urls=(),
                documents=(),
                warnings=(
                    f"No sufficiently exact AMISTA catalog match was found for fund: {fund.name}",
                ),
            )

        return DomainAdapterResult(
            adapter_name=self.name,
            navigation_urls=(matched_entry.url,),
            documents=(),
            warnings=(),
        )


def parse_amista_catalog(
    *,
    body: bytes,
    page_url: str,
) -> tuple[AmistaCatalogEntry, ...]:
    if not body:
        return ()

    parser = LexborHTMLParser(decode_html_bytes(body))
    entries_by_url: dict[str, AmistaCatalogEntry] = {}

    for node in parser.css("a[href]"):
        raw_href = node.attributes.get("href")

        if not raw_href:
            continue

        resolved_url = resolve_link_url(
            base_url=page_url,
            raw_href=raw_href,
        )

        if resolved_url is None or canonical_domain(resolved_url) != AMISTA_DOMAIN:
            continue

        normalized_url = canonical_url(resolved_url)
        path = normalized_url.casefold()

        if path.rstrip("/") == AMISTA_CATALOG_URL.rstrip("/"):
            continue

        if not path.endswith(".html") and ".html?" not in path:
            continue

        if any(
            excluded in path
            for excluded in (
                "fondove-sluzby",
                "kontakty",
                "kariera",
            )
        ):
            continue

        text = _node_text(node)
        key = _entity_key(text)

        if not key:
            continue

        candidate = AmistaCatalogEntry(
            url=resolved_url,
            text=text,
            key=key,
            tokens=frozenset(key.split()),
        )

        current = entries_by_url.get(normalized_url)

        if current is None or len(candidate.text) > len(current.text):
            entries_by_url[normalized_url] = candidate

    return tuple(sorted(entries_by_url.values(), key=lambda item: item.url))


def match_amista_fund(
    *,
    entries: tuple[AmistaCatalogEntry, ...],
    fund_name: str,
) -> AmistaCatalogEntry | None:
    target_key = _entity_key(fund_name)
    target_tokens = frozenset(target_key.split())

    if not target_key or not target_tokens:
        return None

    best_entry: AmistaCatalogEntry | None = None
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

    minimum_score = 0.9 if len(target_tokens) == 1 else 0.72

    if best_entry is None or best_score < minimum_score:
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
    sequence_ratio = SequenceMatcher(None, target_key, candidate_key).ratio()

    if len(target_tokens) == 1:
        target_token = next(iter(target_tokens))

        if target_token not in candidate_tokens or not candidate_key.startswith(target_token):
            return 0.0

    if target_coverage < 0.66:
        return 0.0

    containment_bonus = 0.16 if target_tokens <= candidate_tokens else 0.0
    reverse_containment_bonus = 0.08 if candidate_tokens <= target_tokens else 0.0

    return min(
        1.0,
        (target_coverage * 0.55)
        + (candidate_coverage * 0.15)
        + (sequence_ratio * 0.22)
        + containment_bonus
        + reverse_containment_bonus,
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
