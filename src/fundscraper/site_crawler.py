from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from fundscraper.html_discovery import (
    NEGATIVE_KEYWORDS,
    classify_link,
    decode_html_bytes,
    is_direct_document_url,
    normalize_search_text,
    resolve_link_url,
)
from fundscraper.normalization import (
    canonical_domain,
    canonical_url,
)
from fundscraper.output_models import DocumentType

GENERIC_FUND_NAME_TOKENS = frozenset(
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
        "investments",
        "invest",
        "kapitalem",
        "promennym",
        "uzavreny",
        "spolecnost",
        "company",
        "cesky",
        "czech",
    }
)


NAVIGATION_KEYWORDS = (
    "fond",
    "fund",
    "portfolio",
    "investice",
    "investment",
    "investor",
    "dokument",
    "document",
    "ke stazeni",
    "download",
    "produkty",
    "products",
    "nase fondy",
    "our funds",
    "pro investory",
    "for investors",
    "povinne informace",
    "informacni povinnost",
)


@dataclass(frozen=True, slots=True)
class NavigationLink:
    url: str
    text: str
    score: int
    matched_fund_tokens: tuple[str, ...]
    detected_document_type: DocumentType | None


def fund_name_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    """Return significant normalized tokens from a legal fund name."""

    normalized_name = normalize_search_text(fund_name)

    tokens = re.findall(
        r"[a-z0-9]+",
        normalized_name,
    )

    unique_tokens: list[str] = []

    for token in tokens:
        if len(token) < 3:
            continue

        if token in GENERIC_FUND_NAME_TOKENS:
            continue

        if token not in unique_tokens:
            unique_tokens.append(token)

    return tuple(unique_tokens)


def discover_navigation_links(
    *,
    body: bytes,
    page_url: str,
    effective_base_url: str,
    fund_name: str,
) -> tuple[NavigationLink, ...]:
    """
    Find same-domain HTML pages worth crawling.

    A page is selected when it looks like a document section, investor
    section, fund page, or contains significant tokens from the fund name.
    """

    parser = LexborHTMLParser(decode_html_bytes(body))

    significant_tokens = fund_name_tokens(fund_name)

    links_by_url: dict[
        str,
        NavigationLink,
    ] = {}

    for node in parser.css("a[href]"):
        raw_href = node.attributes.get("href")

        if not raw_href:
            continue

        resolved_url = resolve_link_url(
            base_url=effective_base_url,
            raw_href=raw_href,
        )

        if resolved_url is None:
            continue

        if canonical_domain(resolved_url) != canonical_domain(page_url):
            continue

        if is_direct_document_url(resolved_url):
            continue

        if canonical_url(resolved_url) == canonical_url(page_url):
            continue

        visible_text = anchor_text(node)

        normalized_text = normalize_search_text(visible_text)

        normalized_url = normalize_search_text(resolved_url)

        combined_text = f"{normalized_text} {normalized_url}"

        if any(keyword in combined_text for keyword in NEGATIVE_KEYWORDS):
            continue

        matched_tokens = tuple(token for token in significant_tokens if token in combined_text)

        classified_candidate = classify_link(
            url=resolved_url,
            text=visible_text,
            page_url=page_url,
        )

        score = 0

        if matched_tokens:
            score += min(
                len(matched_tokens) * 30,
                90,
            )

        if any(keyword in combined_text for keyword in NAVIGATION_KEYWORDS):
            score += 25

        detected_document_type: DocumentType | None = None

        if classified_candidate is not None:
            score += min(
                classified_candidate.score,
                60,
            )

            detected_document_type = classified_candidate.document_type

        if score < 25:
            continue

        candidate = NavigationLink(
            url=resolved_url,
            text=visible_text,
            score=score,
            matched_fund_tokens=matched_tokens,
            detected_document_type=(detected_document_type),
        )

        key = canonical_url(candidate.url)

        existing = links_by_url.get(key)

        if existing is None or candidate.score > existing.score:
            links_by_url[key] = candidate

    return tuple(
        sorted(
            links_by_url.values(),
            key=lambda item: (
                -item.score,
                item.url,
            ),
        )
    )


def anchor_text(
    node: Any,
) -> str:
    visible_text = node.text(
        separator=" ",
        strip=True,
    )

    title = node.attributes.get("title") or ""

    aria_label = node.attributes.get("aria-label") or ""

    return re.sub(
        r"\s+",
        " ",
        " ".join(
            (
                visible_text,
                title,
                aria_label,
            )
        ),
    ).strip()
