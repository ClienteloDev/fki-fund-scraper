"""
Sitemap and robots.txt discovery of official fund pages.

A fund website publishes its own map of itself. Reading it reaches the
document archive of a fund without having to guess the navigation, and it
finds pages that no menu links to at all, such as the yearly folders of
an annual-report archive.

Three shapes are supported, because Czech fund sites use all of them:
a plain ``<urlset>``, a ``<sitemapindex>`` pointing at further sitemaps,
and a ``robots.txt`` that names sitemaps at addresses other than
``/sitemap.xml``.

The parsing here is deliberately tolerant. A sitemap that is truncated,
served with the wrong content type or carrying an unexpected namespace
still yields the URLs it does contain, because a partial map is more
useful to discovery than an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from fundscraper.discovery_priority import (
    NAVIGATION_THRESHOLD,
    PrioritySignals,
    score_link,
)
from fundscraper.html_discovery import is_direct_document_url
from fundscraper.normalization import canonical_domain, canonical_url

# The well known location every crawler tries first.
DEFAULT_SITEMAP_PATHS: Final[tuple[str, ...]] = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
)


ROBOTS_PATH: Final = "/robots.txt"


# A sitemap index may point at further indexes. Two levels reach every
# real fund site while keeping a malformed loop bounded.
MAXIMUM_SITEMAP_DEPTH: Final = 2


# How many sitemap documents are fetched for one fund. A large corporate
# site splits its map into hundreds of files, and discovery is not meant
# to read the whole site.
MAXIMUM_SITEMAP_DOCUMENTS: Final = 12


# How many URLs are read out of one sitemap document.
MAXIMUM_URLS_PER_SITEMAP: Final = 5_000


ROBOTS_SITEMAP_PATTERN: Final = re.compile(
    r"^\s*sitemap\s*:\s*(?P<url>\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


LOCATION_TAG_PATTERN: Final = re.compile(
    r"<\s*loc\s*>(?P<url>.*?)<\s*/\s*loc\s*>",
    re.IGNORECASE | re.DOTALL,
)


LAST_MODIFIED_PATTERN: Final = re.compile(
    r"<\s*lastmod\s*>\s*(?P<value>\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """One URL a sitemap offers."""

    url: str
    last_modified: date | None = None


@dataclass(frozen=True, slots=True)
class SitemapDocument:
    """The result of reading one sitemap file."""

    url: str
    is_index: bool
    entries: tuple[SitemapEntry, ...]
    nested_sitemaps: tuple[str, ...]


def robots_sitemap_urls(
    *,
    body: bytes,
    base_url: str,
) -> tuple[str, ...]:
    """
    Return the sitemaps a robots.txt names.

    The directive is case insensitive and may appear anywhere in the
    file, including before any user-agent group.
    """

    text = _decode(body)

    urls: list[str] = []

    for match in ROBOTS_SITEMAP_PATTERN.finditer(text):
        resolved = urljoin(
            base_url,
            match.group("url").strip(),
        )

        if resolved not in urls and _is_http(resolved):
            urls.append(resolved)

    return tuple(urls)


def robots_disallowed_paths(
    *,
    body: bytes,
    user_agent: str = "*",
) -> tuple[str, ...]:
    """
    Return the path prefixes robots.txt closes to this crawler.

    Only the group addressing every crawler and the group naming this one
    are read. Discovery now reaches much deeper into a site than before,
    and a section the operator asked crawlers to leave alone must stay
    out of that reach.
    """

    disallowed: list[str] = []

    applies = False

    for raw_line in _decode(body).splitlines():
        line = raw_line.split("#", 1)[0].strip()

        if not line or ":" not in line:
            continue

        field, _, value = line.partition(":")

        key = field.strip().casefold()

        value = value.strip()

        if key == "user-agent":
            agent = value.casefold()

            applies = agent == "*" or agent in user_agent.casefold()

            continue

        if key == "disallow" and applies and value and value not in disallowed:
            disallowed.append(value)

    return tuple(disallowed)


def is_allowed(
    *,
    url: str,
    disallowed_paths: tuple[str, ...],
) -> bool:
    """Return whether robots.txt leaves a URL open to this crawler."""

    path = urlsplit(url).path or "/"

    for rule in disallowed_paths:
        if rule == "/":
            return False

        if path.startswith(rule):
            return False

    return True


def parse_sitemap(
    *,
    body: bytes,
    sitemap_url: str,
) -> SitemapDocument:
    """
    Read one sitemap file into its URLs or its nested sitemaps.

    XML parsing is attempted first because it distinguishes an index from
    a URL set reliably. A file that does not parse is still scanned for
    location tags, since a truncated sitemap usually holds most of them.
    """

    text = _decode(body)

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return _parse_by_pattern(
            text=text,
            sitemap_url=sitemap_url,
        )

    tag = _local_name(root.tag)

    entries: list[SitemapEntry] = []

    nested: list[str] = []

    for child in root:
        location = _child_text(
            child,
            "loc",
        )

        if location is None:
            continue

        resolved = urljoin(
            sitemap_url,
            location,
        )

        if not _is_http(resolved):
            continue

        if _local_name(child.tag) == "sitemap" or tag == "sitemapindex":
            if resolved not in nested:
                nested.append(resolved)

            continue

        entries.append(
            SitemapEntry(
                url=resolved,
                last_modified=_parse_date(
                    _child_text(
                        child,
                        "lastmod",
                    )
                ),
            )
        )

    return SitemapDocument(
        url=sitemap_url,
        is_index=bool(nested) and not entries,
        entries=tuple(entries[:MAXIMUM_URLS_PER_SITEMAP]),
        nested_sitemaps=tuple(nested[:MAXIMUM_SITEMAP_DOCUMENTS]),
    )


def _parse_by_pattern(
    *,
    text: str,
    sitemap_url: str,
) -> SitemapDocument:
    """Recover the locations of a sitemap that does not parse as XML."""

    is_index = "<sitemapindex" in text.casefold()

    locations: list[str] = []

    for match in LOCATION_TAG_PATTERN.finditer(text):
        resolved = urljoin(
            sitemap_url,
            match.group("url").strip(),
        )

        if _is_http(resolved) and resolved not in locations:
            locations.append(resolved)

    if is_index:
        return SitemapDocument(
            url=sitemap_url,
            is_index=True,
            entries=(),
            nested_sitemaps=tuple(locations[:MAXIMUM_SITEMAP_DOCUMENTS]),
        )

    return SitemapDocument(
        url=sitemap_url,
        is_index=False,
        entries=tuple(SitemapEntry(url=item) for item in locations[:MAXIMUM_URLS_PER_SITEMAP]),
        nested_sitemaps=(),
    )


@dataclass(frozen=True, slots=True)
class RelevantSitemapUrl:
    """A sitemap URL that survived the relevance filter."""

    url: str
    score: int
    last_modified: date | None
    is_document: bool


def filter_sitemap_entries(
    *,
    entries: tuple[SitemapEntry, ...],
    fund_name: str,
    official_domain: str,
    subfund_name: str = "",
    minimum_score: int = NAVIGATION_THRESHOLD,
    limit: int = 40,
) -> tuple[RelevantSitemapUrl, ...]:
    """
    Keep the sitemap URLs worth crawling for one fund.

    A sitemap of a manager lists every page of every fund it runs.
    Crawling all of them would spend the whole budget on other funds, so
    an entry has to earn its place through the same priority signals a
    navigation link does.
    """

    scored: list[RelevantSitemapUrl] = []

    seen: set[str] = set()

    for entry in entries:
        if canonical_domain(entry.url) != official_domain:
            continue

        key = canonical_url(entry.url)

        if key in seen:
            continue

        seen.add(key)

        score = score_link(
            PrioritySignals(
                url=entry.url,
                fund_name=fund_name,
                subfund_name=subfund_name,
            )
        )

        if score.value < minimum_score:
            continue

        scored.append(
            RelevantSitemapUrl(
                url=entry.url,
                score=score.value,
                last_modified=entry.last_modified,
                is_document=is_direct_document_url(entry.url),
            )
        )

    scored.sort(
        key=lambda item: (
            -item.score,
            item.last_modified or date.min,
            item.url,
        ),
    )

    return tuple(scored[:limit])


def candidate_sitemap_urls(
    base_url: str,
) -> tuple[str, ...]:
    """Return the sitemap addresses to try for one site."""

    parts = urlsplit(base_url)

    if not parts.scheme or not parts.netloc:
        return ()

    root = f"{parts.scheme}://{parts.netloc}"

    return tuple(f"{root}{path}" for path in DEFAULT_SITEMAP_PATHS)


def robots_url(
    base_url: str,
) -> str | None:
    """Return the robots.txt address of one site."""

    parts = urlsplit(base_url)

    if not parts.scheme or not parts.netloc:
        return None

    return f"{parts.scheme}://{parts.netloc}{ROBOTS_PATH}"


def _decode(
    body: bytes,
) -> str:
    for encoding in (
        "utf-8",
        "cp1250",
        "iso-8859-2",
    ):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue

    return body.decode(
        "utf-8",
        errors="replace",
    )


def _local_name(
    tag: str,
) -> str:
    return tag.rsplit(
        "}",
        1,
    )[-1].casefold()


def _child_text(
    element: ElementTree.Element,
    name: str,
) -> str | None:
    for child in element:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()

    return None


def _parse_date(
    raw_value: str | None,
) -> date | None:
    if not raw_value:
        return None

    match = LAST_MODIFIED_PATTERN.search(f"<lastmod>{raw_value}")

    if match is None:
        return None

    try:
        return date.fromisoformat(match.group("value"))
    except ValueError:
        return None


def _is_http(
    url: str,
) -> bool:
    return urlsplit(url).scheme in {
        "http",
        "https",
    }
