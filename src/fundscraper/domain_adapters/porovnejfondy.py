from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from selectolax.lexbor import LexborHTMLParser

from fundscraper.domain_adapters.base import (
    DomainAdapterResult,
)
from fundscraper.html_discovery import (
    normalize_search_text,
    resolve_link_url,
)
from fundscraper.http_client import (
    FetchError,
    HttpFetcher,
)
from fundscraper.models import FundInput
from fundscraper.normalization import (
    canonical_domain,
    canonical_url,
)

POROVNEJ_FONDY_DOMAIN = "porovnejfondy.cz"
POROVNEJ_FONDY_INDEX_URL = "https://www.porovnejfondy.cz/fond/"

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
class PorovnejFondyCatalogEntry:
    url: str
    text: str
    key: str
    tokens: frozenset[str]


class PorovnejFondyAdapter:
    """
    Locate an exact fund detail page in the PorovnejFondy.cz catalog.

    The adapter intentionally returns only the matched detail page as a
    navigation seed. The normal crawler and extraction safeguards remain
    responsible for interpreting the page. In particular, historical
    performance must not be treated as a target return.
    """

    name = "porovnejfondy"

    def __init__(self) -> None:
        self._cached_entries: tuple[PorovnejFondyCatalogEntry, ...] | None = None

    def supports(
        self,
        fund: FundInput,
    ) -> bool:
        return canonical_domain(fund.web or "") == POROVNEJ_FONDY_DOMAIN

    async def discover(
        self,
        *,
        fund: FundInput,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> DomainAdapterResult:
        warnings: list[str] = []

        entries = self._cached_entries

        if entries is None or force:
            try:
                result = await fetcher.fetch(
                    POROVNEJ_FONDY_INDEX_URL,
                    force=force,
                )
            except FetchError as exc:
                return DomainAdapterResult(
                    adapter_name=self.name,
                    navigation_urls=(),
                    documents=(),
                    warnings=(f"PorovnejFondy catalog could not be downloaded: {exc.code}: {exc}",),
                )

            if result.content_type not in {
                "text/html",
                "application/xhtml+xml",
                None,
            }:
                return DomainAdapterResult(
                    adapter_name=self.name,
                    navigation_urls=(),
                    documents=(),
                    warnings=(
                        "PorovnejFondy catalog returned an unexpected "
                        f"content type: {result.content_type}",
                    ),
                )

            entries = parse_porovnejfondy_catalog(
                body=result.body,
                page_url=result.final_url,
            )

            if not force:
                self._cached_entries = entries

        matched_entry = match_porovnejfondy_fund(
            entries=entries,
            fund_name=fund.name,
        )

        if matched_entry is None:
            warnings.append(
                f"No sufficiently exact PorovnejFondy catalog match was found for fund: {fund.name}"
            )

            return DomainAdapterResult(
                adapter_name=self.name,
                navigation_urls=(),
                documents=(),
                warnings=tuple(warnings),
            )

        return DomainAdapterResult(
            adapter_name=self.name,
            navigation_urls=(matched_entry.url,),
            documents=(),
            warnings=tuple(warnings),
        )


def parse_porovnejfondy_catalog(
    *,
    body: bytes,
    page_url: str,
) -> tuple[PorovnejFondyCatalogEntry, ...]:
    """Parse unique fund detail links from the server-rendered catalog."""

    if not body:
        return ()

    parser = LexborHTMLParser(body)

    entries_by_url: dict[str, PorovnejFondyCatalogEntry] = {}

    for node in parser.css("a[href]"):
        raw_href = node.attributes.get("href")

        if not raw_href:
            continue

        resolved_url = resolve_link_url(
            base_url=page_url,
            raw_href=raw_href,
        )

        if resolved_url is None:
            continue

        if canonical_domain(resolved_url) != POROVNEJ_FONDY_DOMAIN:
            continue

        normalized_url = canonical_url(resolved_url)

        if normalized_url.rstrip("/") == POROVNEJ_FONDY_INDEX_URL.rstrip("/"):
            continue

        if "/fond/" not in normalized_url.casefold():
            continue

        text = _node_text(node)

        # Prefer the visible legal fund name. Including the complete URL
        # pollutes an exact name match with slug, domain and ISIN tokens.
        key = _entity_key(text)

        # Some catalog links can contain no visible text. In that case,
        # retain the URL as a conservative fallback.
        if not key:
            key = _entity_key(resolved_url)

        tokens = frozenset(key.split())

        if not key or not tokens:
            continue

        current = entries_by_url.get(normalized_url)

        candidate = PorovnejFondyCatalogEntry(
            url=resolved_url,
            text=text,
            key=key,
            tokens=tokens,
        )

        if current is None or len(candidate.text) > len(current.text):
            entries_by_url[normalized_url] = candidate

    return tuple(
        sorted(
            entries_by_url.values(),
            key=lambda item: item.url,
        )
    )


def match_porovnejfondy_fund(
    *,
    entries: tuple[PorovnejFondyCatalogEntry, ...],
    fund_name: str,
) -> PorovnejFondyCatalogEntry | None:
    """Return only a high-confidence catalog match for the legal fund name."""

    target_key = _entity_key(fund_name)
    target_tokens = frozenset(target_key.split())

    if not target_key or not target_tokens:
        return None

    best_entry: PorovnejFondyCatalogEntry | None = None
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

    minimum_score = 0.88 if len(target_tokens) == 1 else 0.74

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
    sequence_ratio = SequenceMatcher(
        None,
        target_key,
        candidate_key,
    ).ratio()

    if len(target_tokens) == 1:
        target_token = next(iter(target_tokens))

        if target_token not in candidate_tokens:
            return 0.0

        if not candidate_key.startswith(target_token):
            return 0.0

    if target_coverage < 0.66:
        return 0.0

    substring_bonus = 0.12 if target_key in candidate_key else 0.0

    return min(
        1.0,
        (target_coverage * 0.58)
        + (candidate_coverage * 0.17)
        + (sequence_ratio * 0.25)
        + substring_bonus,
    )


def _entity_key(
    value: str,
) -> str:
    normalized = normalize_search_text(value)

    tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    result: list[str] = []

    for token in tokens:
        if token in GENERIC_FUND_TOKENS:
            continue

        if len(token) < 2:
            continue

        if token.isdigit():
            continue

        if token not in result:
            result.append(token)

    return " ".join(result)


def _node_text(
    node: object,
) -> str:
    text_method = getattr(node, "text", None)

    if not callable(text_method):
        return ""

    text = text_method(
        separator=" ",
        strip=True,
    )

    return re.sub(
        r"\s+",
        " ",
        str(text),
    ).strip()
