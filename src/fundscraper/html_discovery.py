from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from fundscraper.normalization import (
    canonical_domain,
    canonical_url,
)
from fundscraper.output_models import DocumentType

DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".xhtml",
        ".xml",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
    }
)


SKIPPED_URL_PREFIXES = (
    "#",
    "mailto:",
    "tel:",
    "javascript:",
    "data:",
    "blob:",
    "file:",
    "ftp:",
)


@dataclass(frozen=True, slots=True)
class DiscoveryRule:
    document_type: DocumentType
    base_score: int
    keywords: tuple[str, ...]


DISCOVERY_RULES = (
    DiscoveryRule(
        document_type=DocumentType.PRIIPS_KID,
        base_score=120,
        keywords=(
            "priips",
            "key information document",
            "sdeleni klicovych informaci",
            "klicove informace",
            "kid dokument",
            "kid pdf",
            "kiid",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.SUBFUND_STATUTE,
        base_score=100,
        keywords=(
            "statut podfondu",
            "subfund statute",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.STATUTE,
        base_score=95,
        keywords=(
            "statut fondu",
            "statut",
            "fund statute",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.MEMORANDUM,
        base_score=90,
        keywords=(
            "memorandum",
            "investicni memorandum",
            "information memorandum",
            "offering memorandum",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.FINANCIAL_STATEMENTS,
        base_score=85,
        keywords=(
            "ucetni zaverka",
            "financial statements",
            "financial statement",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.ANNUAL_REPORT,
        base_score=80,
        keywords=(
            "vyrocni zprava",
            "annual report",
            "annual financial report",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.HALF_YEAR_REPORT,
        base_score=75,
        keywords=(
            "pololetni zprava",
            "pololetni report",
            "half year report",
            "semi annual report",
            "interim report",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.FACTSHEET,
        base_score=70,
        keywords=(
            "factsheet",
            "fact sheet",
            "informacni list",
            "produktovy list",
            "fund sheet",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.INFOLETTER,
        base_score=65,
        keywords=(
            "infoletter",
            "newsletter",
            "mesicni zprava",
            "kvartalni zprava",
            "monthly report",
            "quarterly report",
        ),
    ),
    DiscoveryRule(
        document_type=DocumentType.OTHER,
        base_score=45,
        keywords=(
            "dokumenty",
            "documents",
            "ke stazeni",
            "download center",
            "pro investory",
            "for investors",
        ),
    ),
)


NEGATIVE_KEYWORDS = (
    "gdpr",
    "privacy",
    "cookies",
    "ochrana osobnich udaju",
    "kariera",
    "career",
    "kontakt",
    "contact",
    "fotogalerie",
    "gallery",
    "press release",
    "tiskova zprava",
)


DOWNLOAD_KEYWORDS = (
    "stahnout",
    "ke stazeni",
    "download",
    "soubor",
    "dokument",
    "document",
    "pdf",
)


@dataclass(frozen=True, slots=True)
class DiscoveredLink:
    url: str
    text: str
    score: int
    document_type: DocumentType
    same_domain: bool
    direct_document: bool


@dataclass(frozen=True, slots=True)
class ParsedHtmlPage:
    page_url: str
    effective_base_url: str
    canonical_page_url: str | None
    title: str | None
    links_total: int
    candidates: tuple[DiscoveredLink, ...]


class HtmlDiscoveryError(ValueError):
    """Raised when an HTML page cannot be processed."""

    code = "html_discovery_error"


def is_html_response(
    *,
    content_type: str | None,
    body: bytes,
) -> bool:
    """Return whether the downloaded response appears to contain HTML."""

    if content_type in {
        "text/html",
        "application/xhtml+xml",
    }:
        return True

    prefix = body[:1024].lstrip().lower()

    return (
        prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or prefix.startswith(b"<?xml")
        or b"<html" in prefix
    )


def parse_html_page(
    *,
    body: bytes,
    page_url: str,
) -> ParsedHtmlPage:
    """Parse one HTML page and return ranked document candidates."""

    if not body:
        raise HtmlDiscoveryError("HTML response body is empty")

    parser = LexborHTMLParser(body)

    title = _extract_title(parser)
    effective_base_url = _extract_base_url(
        parser,
        page_url,
    )

    canonical_page_url = _extract_canonical_url(
        parser,
        effective_base_url,
    )

    discovered_by_url: dict[
        str,
        DiscoveredLink,
    ] = {}

    links_total = 0

    for node in parser.css("a[href]"):
        raw_href = node.attributes.get("href")

        if raw_href is None:
            continue

        raw_href = raw_href.strip()

        if not raw_href:
            continue

        links_total += 1

        resolved_url = resolve_link_url(
            base_url=effective_base_url,
            raw_href=raw_href,
        )

        if resolved_url is None:
            continue

        if canonical_url(resolved_url) == canonical_url(page_url):
            continue

        text = _anchor_text(node)

        candidate = classify_link(
            url=resolved_url,
            text=text,
            page_url=page_url,
            has_download_attribute=("download" in node.attributes),
        )

        if candidate is None:
            continue

        candidate_key = canonical_url(candidate.url)

        existing_candidate = discovered_by_url.get(candidate_key)

        if existing_candidate is None or candidate.score > existing_candidate.score:
            discovered_by_url[candidate_key] = candidate

    candidates = tuple(
        sorted(
            discovered_by_url.values(),
            key=lambda item: (
                -item.score,
                item.url,
            ),
        )
    )

    return ParsedHtmlPage(
        page_url=page_url,
        effective_base_url=effective_base_url,
        canonical_page_url=canonical_page_url,
        title=title,
        links_total=links_total,
        candidates=candidates,
    )


def resolve_link_url(
    *,
    base_url: str,
    raw_href: str,
) -> str | None:
    """Convert an HTML href into a usable absolute HTTP URL."""

    stripped_href = raw_href.strip()

    if not stripped_href:
        return None

    lowered_href = stripped_href.casefold()

    if lowered_href.startswith(SKIPPED_URL_PREFIXES):
        return None

    resolved_url = urljoin(
        base_url,
        stripped_href,
    )

    parsed = urlsplit(resolved_url)

    if parsed.scheme.lower() not in {
        "http",
        "https",
    }:
        return None

    if parsed.hostname is None:
        return None

    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def classify_link(
    *,
    url: str,
    text: str,
    page_url: str,
    has_download_attribute: bool = False,
) -> DiscoveredLink | None:
    """Classify and rank one discovered link."""

    normalized_text = normalize_search_text(text)

    normalized_url = normalize_search_text(url)

    search_text = (f"{normalized_text} {normalized_url}").strip()

    best_type = DocumentType.OTHER
    best_score = 0

    for rule in DISCOVERY_RULES:
        matches = sum(1 for keyword in rule.keywords if keyword in search_text)

        if matches == 0:
            continue

        rule_score = (
            rule.base_score
            + min(
                matches - 1,
                3,
            )
            * 5
        )

        if rule_score > best_score:
            best_score = rule_score
            best_type = rule.document_type

    direct_document = is_direct_document_url(url)

    if direct_document:
        best_score += 30

    if any(keyword in normalized_text for keyword in DOWNLOAD_KEYWORDS):
        best_score += 10

    if has_download_attribute:
        best_score += 10

    same_domain = canonical_domain(url) == canonical_domain(page_url)

    if same_domain:
        best_score += 5

    if any(keyword in search_text for keyword in NEGATIVE_KEYWORDS):
        best_score -= 70

    best_score = max(
        best_score,
        0,
    )

    if best_score < 35:
        return None

    return DiscoveredLink(
        url=url,
        text=text,
        score=best_score,
        document_type=best_type,
        same_domain=same_domain,
        direct_document=direct_document,
    )


def is_direct_document_url(
    url: str,
) -> bool:
    """Return whether the URL path has a supported document extension."""

    path = urlsplit(url).path

    suffix = PurePosixPath(path).suffix.casefold()

    return suffix in DOCUMENT_EXTENSIONS


def normalize_search_text(
    value: str,
) -> str:
    """Normalize text for keyword matching."""

    decomposed = unicodedata.normalize(
        "NFKD",
        value,
    )

    without_diacritics = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )

    lowered = without_diacritics.casefold()

    return re.sub(
        r"\s+",
        " ",
        lowered,
    ).strip()


def _extract_title(
    parser: LexborHTMLParser,
) -> str | None:
    title_nodes = parser.css("title")

    if not title_nodes:
        return None

    title = title_nodes[0].text(
        separator=" ",
        strip=True,
    )

    normalized_title = _normalize_visible_text(title)

    return normalized_title or None


def _extract_base_url(
    parser: LexborHTMLParser,
    page_url: str,
) -> str:
    base_nodes = parser.css("base[href]")

    if not base_nodes:
        return page_url

    raw_href = base_nodes[0].attributes.get("href")

    if not raw_href:
        return page_url

    resolved_url = resolve_link_url(
        base_url=page_url,
        raw_href=raw_href,
    )

    return resolved_url or page_url


def _extract_canonical_url(
    parser: LexborHTMLParser,
    base_url: str,
) -> str | None:
    for node in parser.css("link[rel][href]"):
        raw_rel = node.attributes.get("rel") or ""

        rel_values = {value.casefold() for value in raw_rel.split()}

        if "canonical" not in rel_values:
            continue

        raw_href = node.attributes.get("href")

        if raw_href is None:
            continue

        return resolve_link_url(
            base_url=base_url,
            raw_href=raw_href,
        )

    return None


def _anchor_text(
    node: Any,
) -> str:
    visible_text = node.text(
        separator=" ",
        strip=True,
    )

    title = node.attributes.get("title") or ""

    aria_label = node.attributes.get("aria-label") or ""

    return _normalize_visible_text(
        " ".join(
            (
                visible_text,
                title,
                aria_label,
            )
        )
    )


def _normalize_visible_text(
    value: str,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()
