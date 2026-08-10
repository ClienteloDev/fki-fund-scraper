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
    decode_html_bytes,
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


AVANT_MANAGED_EXTERNAL_FUND_KEYS = frozenset(
    {
        "nemomax",
        "spilberk",
    }
)


AVANT_CATALOG_URLS = (
    "https://www.avantfunds.cz/informace-o-fondech/",
    "https://www.avantfunds.cz/informacni-povinnost/",
)


AVANT_DIRECT_FUND_URLS = {
    "spilberk": ("https://www.avantfunds.cz/fondy/spilberk-investicni-fond-sicav-a-s/"),
    "nemomax": (
        "https://www.avantfunds.cz/"
        "fondy/"
        "nemomax-investicni-fond-"
        "s-promennym-zakladnim-kapitalem-a-s-2/"
    ),
}


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
        "kapital",
        "kapitalem",
        "zakladni",
        "zakladnim",
        "zakladniho",
        "promenny",
        "promennym",
        "promenneho",
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
    """
    Discover documents belonging to one exact fund on AVANT pages.

    The direct fund page can contain investment information but usually
    does not contain downloadable documents. The central AVANT catalogs
    are therefore processed as the authoritative document source.

    For externally hosted funds, AVANT navigation links are not returned
    to the generic crawler. Only relevant discovered documents are added.
    """

    name = "avantfunds"

    def supports(
        self,
        fund: FundInput,
    ) -> bool:
        domain = canonical_domain(fund.web or "")

        if domain == AVANT_DOMAIN:
            return True

        fund_key = _entity_key(fund.name)

        return fund_key in AVANT_MANAGED_EXTERNAL_FUND_KEYS

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

        fund_key = _entity_key(fund.name)

        direct_fund_url = AVANT_DIRECT_FUND_URLS.get(fund_key)

        source_pages: list[
            tuple[
                str,
                bool,
            ]
        ] = []

        if direct_fund_url is not None:
            source_pages.append(
                (
                    direct_fund_url,
                    True,
                )
            )

        for catalog_url in AVANT_CATALOG_URLS:
            source_pages.append(
                (
                    catalog_url,
                    False,
                )
            )

        unique_source_pages: list[
            tuple[
                str,
                bool,
            ]
        ] = []

        seen_source_urls: set[str] = set()

        for source_url, allow_root_fallback in source_pages:
            source_key = canonical_url(source_url)

            if source_key in seen_source_urls:
                continue

            seen_source_urls.add(source_key)

            unique_source_pages.append(
                (
                    source_url,
                    allow_root_fallback,
                )
            )

        for (
            source_url,
            allow_root_fallback,
        ) in unique_source_pages:
            try:
                result = await fetcher.fetch(
                    source_url,
                    force=force,
                )
            except FetchError as exc:
                warnings.append(
                    f"AVANT page could not be downloaded: {source_url}: {exc.code}: {exc}"
                )

                continue

            if result.content_type not in {
                "text/html",
                "application/xhtml+xml",
                None,
            }:
                warnings.append(
                    "AVANT page returned an unexpected "
                    f"content type: {source_url}: "
                    f"{result.content_type}"
                )

                continue

            page_discovery = parse_avant_catalog_page(
                body=result.body,
                page_url=result.final_url,
                fund_name=fund.name,
                allow_root_fallback=(allow_root_fallback),
            )

            if page_discovery is None:
                if not allow_root_fallback:
                    warnings.append(
                        "The exact fund document section "
                        "was not found on AVANT page: "
                        f"{result.final_url}"
                    )

                continue

            (
                navigation_urls,
                documents,
            ) = page_discovery

            for navigation_url in navigation_urls:
                key = canonical_url(navigation_url)

                navigation_by_url[key] = navigation_url

            for document in documents:
                key = canonical_url(document.url)

                existing = documents_by_url.get(key)

                if existing is None or document.score > existing.score:
                    documents_by_url[key] = document

        navigation_urls_result = tuple(sorted(navigation_by_url.values()))

        if canonical_domain(fund.web or "") != AVANT_DOMAIN:
            # The official external website is handled by the generic
            # crawler. AVANT provides documents only.
            navigation_urls_result = ()

        return DomainAdapterResult(
            adapter_name=self.name,
            navigation_urls=(navigation_urls_result),
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
    allow_root_fallback: bool = False,
) -> (
    tuple[
        tuple[str, ...],
        tuple[DiscoveredLink, ...],
    ]
    | None
):
    """
    Extract links belonging to one exact fund section.

    Root fallback is allowed only for a direct fund profile page.
    It is never allowed for the large central catalogs because that
    would collect documents belonging to all AVANT funds.
    """

    if not body:
        return None

    parser = LexborHTMLParser(decode_html_bytes(body))

    container = _find_fund_container(
        parser=parser,
        fund_name=fund_name,
        page_url=page_url,
    )

    if container is None and allow_root_fallback:
        root_node = parser.root

        if root_node is None:
            return None

        page_text = _node_text(root_node)

        if not _matches_fund_name(
            value=page_text,
            target_key=_entity_key(fund_name),
        ):
            return None

        container = root_node

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
                    document_type=(DocumentType.OTHER),
                    same_domain=(canonical_domain(resolved_url) == canonical_domain(page_url)),
                    direct_document=True,
                )

            if not _is_relevant_document(candidate):
                continue

            boosted_candidate = DiscoveredLink(
                url=candidate.url,
                text=candidate.text,
                score=(candidate.score + 40),
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
    page_url: str,
) -> Any | None:
    """
    Find the smallest fund-specific container that contains documents.

    The catalog often stores the fund title in a nested element while
    attachments are placed in a higher parent. Therefore the search walks
    upwards until it reaches an ancestor containing a direct document URL.
    """

    target_key = _entity_key(fund_name)

    matching_nodes: list[Any] = []

    for node in parser.css("h1, h2, h3, h4, h5, h6, button, strong, summary, p, a"):
        node_text = _node_text(node)

        if not _matches_fund_name(
            value=node_text,
            target_key=target_key,
        ):
            continue

        matching_nodes.append(node)

    container_candidates: list[
        tuple[
            int,
            int,
            Any,
        ]
    ] = []

    for matching_node in matching_nodes:
        current: Any | None = matching_node

        for depth in range(12):
            if current is None:
                break

            tag_name = str(current.tag).casefold()

            if tag_name in {
                "section",
                "article",
                "li",
                "details",
                "div",
                "main",
            }:
                document_count = _count_document_links(
                    node=current,
                    page_url=page_url,
                )

                if document_count > 0:
                    container_text = _node_text(current)

                    container_candidates.append(
                        (
                            len(container_text),
                            depth,
                            current,
                        )
                    )

                    # This is the closest ancestor for this matching
                    # heading that already contains document links.
                    break

            current = current.parent

    if container_candidates:
        _, _, best_node = min(
            container_candidates,
            key=lambda item: (
                item[0],
                item[1],
            ),
        )

        return best_node

    # Fallback for catalog structures where the fund name is not stored
    # inside a heading-like element.
    fallback_candidates: list[
        tuple[
            int,
            Any,
        ]
    ] = []

    for node in parser.css("section, article, li, details, div, main"):
        text = _node_text(node)

        if not _matches_fund_name(
            value=text,
            target_key=target_key,
        ):
            continue

        if (
            _count_document_links(
                node=node,
                page_url=page_url,
            )
            == 0
        ):
            continue

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


def _count_document_links(
    *,
    node: Any,
    page_url: str,
) -> int:
    count = 0

    for link_node in node.css("a[href]"):
        if _node_has_document_href(
            node=link_node,
            page_url=page_url,
        ):
            count += 1

    return count


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

    return "/fondy/" in url.casefold() and any(token in normalized_url for token in fund_tokens)


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
