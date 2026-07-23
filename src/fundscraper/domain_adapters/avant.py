from __future__ import annotations

import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from fundscraper.domain_adapters.base import (
    DomainAdapterResult,
)
from fundscraper.html_discovery import (
    DiscoveredLink,
    classify_link,
    is_direct_document_url,
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
from fundscraper.output_models import (
    DocumentType,
)

AVANT_DOMAIN = "avantfunds.cz"

AVANT_CATALOG_URLS = (
    "https://www.avantfunds.cz/informace-o-fondech/",
    "https://www.avantfunds.cz/informacni-povinnost/",
)


GENERIC_ENTITY_TOKENS = frozenset(
    {
        "a",
        "as",
        "s",
        "sicav",
        "fond",
        "fund",
        "fonds",
        "investicni",
        "investment",
        "spolecnost",
        "uzavreny",
        "otevreny",
        "kapitalem",
        "promennym",
    }
)


RELEVANT_DOCUMENT_KEYWORDS = (
    "kid",
    "priips",
    "klicove informace",
    "sdeleni klicovych informaci",
    "statut",
    "memorandum",
    "vyrocni zprava",
    "ucetni zaverka",
    "pololetni zprava",
    "factsheet",
    "informacni list",
    "informacni memorandum",
    "informacni teaser",
    "verejna vyzva",
    "vyzva k upisu",
    "upis",
    "subscription",
    "annual report",
    "financial statements",
)


IRRELEVANT_DOCUMENT_KEYWORDS = (
    "pozvanka",
    "valna hromada",
    "hlasovani",
    "plna moc",
    "oznameni o zmene",
    "rozhodnuti jedineho akcionare",
    "zapis z valne hromady",
    "marketingovych materialu",
    "politika udrzitelnosti",
    "nepriznivych dopadech",
)


class AvantFundsAdapter:
    """Discover exact fund documents in AVANT catalog pages."""

    name = "avantfunds"

    def supports(
        self,
        fund: FundInput,
    ) -> bool:
        return canonical_domain(fund.web) == AVANT_DOMAIN

    async def discover(
        self,
        *,
        fund: FundInput,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> DomainAdapterResult:
        navigation_by_url: dict[
            str,
            str,
        ] = {}

        documents_by_url: dict[
            str,
            DiscoveredLink,
        ] = {}

        warnings: list[str] = []

        for catalog_url in AVANT_CATALOG_URLS:
            try:
                result = await fetcher.fetch(
                    catalog_url,
                    force=force,
                )
            except FetchError as exc:
                warnings.append(
                    f"AVANT catalog could not be downloaded: {catalog_url}: {exc.code}: {exc}"
                )

                continue

            if result.content_type not in {
                "text/html",
                "application/xhtml+xml",
                None,
            }:
                warnings.append(
                    "AVANT catalog returned an unexpected "
                    f"content type: {catalog_url}: "
                    f"{result.content_type}"
                )

                continue

            page_discovery = parse_avant_catalog_page(
                body=result.body,
                page_url=result.final_url,
                fund_name=fund.name,
            )

            if page_discovery is None:
                warnings.append(
                    f"The exact fund section was not found in AVANT catalog: {result.final_url}"
                )

                continue

            navigation_urls, documents = page_discovery

            for navigation_url in navigation_urls:
                navigation_by_url[canonical_url(navigation_url)] = navigation_url

            for document in documents:
                key = canonical_url(document.url)

                existing = documents_by_url.get(key)

                if existing is None or document.score > existing.score:
                    documents_by_url[key] = document

        return DomainAdapterResult(
            adapter_name=self.name,
            navigation_urls=tuple(sorted(navigation_by_url.values())),
            documents=tuple(
                sorted(
                    documents_by_url.values(),
                    key=lambda item: (
                        -item.score,
                        item.url,
                    ),
                )
            ),
            warnings=tuple(warnings),
        )


def parse_avant_catalog_page(
    *,
    body: bytes,
    page_url: str,
    fund_name: str,
) -> (
    tuple[
        tuple[str, ...],
        tuple[DiscoveredLink, ...],
    ]
    | None
):
    """Extract links belonging to one exact fund catalog section."""

    if not body:
        return None

    parser = LexborHTMLParser(body)

    container = _find_fund_container(
        parser=parser,
        fund_name=fund_name,
    )

    if container is None:
        return None

    navigation_by_url: dict[
        str,
        str,
    ] = {}

    documents_by_url: dict[
        str,
        DiscoveredLink,
    ] = {}

    fund_tokens = set(_entity_tokens(fund_name))

    for node in container.css("a[href]"):
        raw_href = node.attributes.get("href")

        if not raw_href:
            continue

        resolved_url = resolve_link_url(
            base_url=page_url,
            raw_href=raw_href,
        )

        if resolved_url is None:
            continue

        link_text = _node_text(node)

        if is_direct_document_url(resolved_url):
            candidate = classify_link(
                url=resolved_url,
                text=link_text,
                page_url=page_url,
                has_download_attribute=("download" in node.attributes),
            )

            if candidate is None:
                candidate = DiscoveredLink(
                    url=resolved_url,
                    text=link_text,
                    score=40,
                    document_type=DocumentType.OTHER,
                    same_domain=(canonical_domain(resolved_url) == canonical_domain(page_url)),
                    direct_document=True,
                )

            if not _is_relevant_document(candidate):
                continue

            boosted_candidate = DiscoveredLink(
                url=candidate.url,
                text=candidate.text,
                score=candidate.score + 40,
                document_type=(candidate.document_type),
                same_domain=(candidate.same_domain),
                direct_document=True,
            )

            key = canonical_url(boosted_candidate.url)

            existing = documents_by_url.get(key)

            if existing is None or boosted_candidate.score > existing.score:
                documents_by_url[key] = boosted_candidate

            continue

        if canonical_domain(resolved_url) != AVANT_DOMAIN:
            continue

        if not _is_fund_navigation_link(
            url=resolved_url,
            text=link_text,
            fund_tokens=fund_tokens,
        ):
            continue

        navigation_by_url[canonical_url(resolved_url)] = resolved_url

    return (
        tuple(sorted(navigation_by_url.values())),
        tuple(
            sorted(
                documents_by_url.values(),
                key=lambda item: (
                    -item.score,
                    item.url,
                ),
            )
        ),
    )


def _find_fund_container(
    *,
    parser: LexborHTMLParser,
    fund_name: str,
) -> Any | None:
    """
    Find the nearest bounded HTML container belonging to the fund.

    The nearest section-like ancestor is preferred over a larger parent
    containing documents of multiple funds.
    """

    target_key = _entity_key(fund_name)

    headings = parser.css("h1, h2, h3, h4, h5, h6, button, strong, summary, p")

    for heading in headings:
        heading_text = _node_text(heading)

        if not _matches_fund_name(
            value=heading_text,
            target_key=target_key,
        ):
            continue

        current: Any | None = heading

        for _ in range(8):
            if current is None:
                break

            tag_name = str(current.tag).casefold()

            links = current.css("a[href]")

            if links and tag_name in {
                "section",
                "article",
                "li",
                "details",
                "div",
            }:
                return current

            current = current.parent

    fallback_candidates: list[tuple[int, Any]] = []

    for node in parser.css("section, article, li, details, div"):
        text = _node_text(node)

        if not _matches_fund_name(
            value=text,
            target_key=target_key,
        ):
            continue

        links = node.css("a[href]")

        if not links:
            continue

        # Prefer the smallest matching container. A large parent often
        # contains sections belonging to several different funds.
        fallback_candidates.append(
            (
                len(text),
                node,
            )
        )

    if not fallback_candidates:
        return None

    _, best_node = min(
        fallback_candidates,
        key=lambda item: item[0],
    )

    return best_node


def _matches_fund_name(
    *,
    value: str,
    target_key: str,
) -> bool:
    if not target_key:
        return False

    value_key = _entity_key(value)

    if not value_key:
        return False

    if value_key == target_key or target_key in value_key:
        return True

    target_tokens = set(target_key.split())

    value_tokens = set(value_key.split())

    return bool(target_tokens) and target_tokens.issubset(value_tokens)


def _entity_key(
    value: str,
) -> str:
    return " ".join(_entity_tokens(value))


def _entity_tokens(
    value: str,
) -> tuple[str, ...]:
    normalized = normalize_search_text(value)

    tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    result: list[str] = []

    for token in tokens:
        if token in GENERIC_ENTITY_TOKENS:
            continue

        if len(token) < 2:
            continue

        if token not in result:
            result.append(token)

    return tuple(result)


def _is_relevant_document(
    candidate: DiscoveredLink,
) -> bool:
    search_text = normalize_search_text(f"{candidate.text} {candidate.url}")

    if any(keyword in search_text for keyword in IRRELEVANT_DOCUMENT_KEYWORDS):
        return False

    if candidate.document_type is not DocumentType.OTHER:
        return True

    return any(keyword in search_text for keyword in RELEVANT_DOCUMENT_KEYWORDS)


def _is_fund_navigation_link(
    *,
    url: str,
    text: str,
    fund_tokens: set[str],
) -> bool:
    combined_tokens = set(_entity_tokens(f"{text} {url}"))

    if fund_tokens and fund_tokens.issubset(combined_tokens):
        return True

    normalized_url = normalize_search_text(url)

    return "/fondy/" in url and any(token in normalized_url for token in fund_tokens)


def _node_has_document_href(
    *,
    node: Any,
    page_url: str,
) -> bool:
    raw_href = node.attributes.get("href")

    if not raw_href:
        return False

    resolved_url = resolve_link_url(
        base_url=page_url,
        raw_href=raw_href,
    )

    return resolved_url is not None and is_direct_document_url(resolved_url)


def _node_text(
    node: Any,
) -> str:
    text = node.text(
        separator=" ",
        strip=True,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()
