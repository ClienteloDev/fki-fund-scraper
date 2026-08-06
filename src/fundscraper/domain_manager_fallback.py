from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Final
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from fundscraper.domain_adapters.amista import AmistaAdapter
from fundscraper.domain_adapters.avant import AvantFundsAdapter
from fundscraper.domain_adapters.base import DomainAdapter
from fundscraper.domain_candidates import (
    CandidateDiscovery,
    CandidateSourceName,
    DomainCandidate,
)
from fundscraper.html_discovery import (
    decode_html_bytes,
    is_html_response,
    normalize_search_text,
    resolve_link_url,
)
from fundscraper.http_client import (
    FetchError,
    HttpFetcher,
)
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain, canonical_url

# Legal form and fund vocabulary that never identifies one exact fund.
GENERIC_FUND_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "akciova",
        "czk",
        "eur",
        "fond",
        "fondu",
        "fondy",
        "fonds",
        "fund",
        "funds",
        "investicni",
        "investment",
        "kapital",
        "kapitalem",
        "podfond",
        "promenneho",
        "promennym",
        "promenny",
        "sicav",
        "spolecnost",
        "subfund",
        "usd",
        "zakladniho",
        "zakladnim",
    }
)


CATALOG_EXCLUDED_PATH_FRAGMENTS: Final[tuple[str, ...]] = (
    "kontakt",
    "kariera",
    "career",
    "o-nas",
    "about",
    "gdpr",
    "cookie",
    "povinne-informace",
    "fondove-sluzby",
    "aktuality",
    "news",
    "blog",
)


# The site each adapter owns. Passing the adapter its own domain keeps it
# on the code path that returns its fund profile URLs.
ADAPTER_HOME_URLS: Final[dict[str, str]] = {
    "avantfunds": "https://www.avantfunds.cz/",
    "amista": "https://www.amista.cz/",
}


DEFAULT_ADAPTER_HOME_URL: Final = "https://www.avantfunds.cz/"


@dataclass(frozen=True, slots=True)
class ManagerCatalog:
    """One fund manager or administrator with a public fund catalog."""

    name: str
    domain: str
    catalog_urls: tuple[str, ...]


# Managers and administrators whose fund catalogs are used when a fund has
# no standalone official website. AVANT is handled by its dedicated
# adapter; the remaining ones share the generic catalog match.
KNOWN_MANAGER_CATALOGS: Final[tuple[ManagerCatalog, ...]] = (
    ManagerCatalog(
        # The AMISTA adapter only follows detail pages. The mandatory
        # information page also lists funds that exist solely as an
        # accordion section with their documents, such as IROMET.
        name="amista",
        domain="amista.cz",
        catalog_urls=("https://www.amista.cz/povinne-informace.html",),
    ),
    ManagerCatalog(
        name="codya",
        domain="codyainvest.cz",
        catalog_urls=("https://www.codyainvest.cz/nase-fondy",),
    ),
    ManagerCatalog(
        name="moneco",
        domain="monecois.cz",
        catalog_urls=("https://monecois.cz/",),
    ),
    ManagerCatalog(
        name="jt",
        domain="jtis.cz",
        catalog_urls=("https://www.jtis.cz/fondy",),
    ),
    ManagerCatalog(
        name="delta",
        domain="deltais.cz",
        catalog_urls=("https://www.deltais.cz/fondy",),
    ),
    ManagerCatalog(
        name="encor",
        domain="encoram.com",
        catalog_urls=("https://encoram.com/encor-fondy",),
    ),
    ManagerCatalog(
        name="tiller",
        domain="tillerfunds.cz",
        catalog_urls=("https://www.tillerfunds.cz/cs/portfolio",),
    ),
    ManagerCatalog(
        name="wood",
        domain="wood.sk",
        catalog_urls=("https://wood.sk/produkty/fondy",),
    ),
    ManagerCatalog(
        name="nwd",
        domain="nwd.cz",
        catalog_urls=("https://nwd.cz/fondy",),
    ),
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One fund profile link found in a manager catalog."""

    url: str
    text: str
    key: str
    tokens: frozenset[str]


class ManagerAdapterFallback:
    """
    Find the exact fund profile on a known manager or administrator site.

    This runs only when no standalone official website was verified. It
    returns the specific fund profile URL, never just the homepage of the
    manager, so that the stored value points at the requested fund.
    """

    name = CandidateSourceName.ADAPTER

    def __init__(
        self,
        *,
        catalogs: tuple[ManagerCatalog, ...] = KNOWN_MANAGER_CATALOGS,
        adapters: tuple[DomainAdapter, ...] | None = None,
        minimum_score: float = 0.72,
    ) -> None:
        self.catalogs = catalogs

        self.adapters = (
            adapters
            if adapters is not None
            else (
                AmistaAdapter(),
                AvantFundsAdapter(),
            )
        )

        self.minimum_score = minimum_score

        # Catalogs are identical for every fund of one run, so they are
        # parsed once instead of once per fund. The HTTP layer already
        # caches the response; this avoids repeating the HTML parsing.
        self._catalog_entries: dict[str, tuple[CatalogEntry, ...]] = {}

        self._catalog_warnings: dict[str, tuple[str, ...]] = {}

        self._catalog_locks: dict[str, asyncio.Lock] = {}

    async def find(
        self,
        *,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> CandidateDiscovery:
        candidates: list[DomainCandidate] = []

        warnings: list[str] = []

        seen_urls: set[str] = set()

        for adapter in self.adapters:
            profile_urls, adapter_warnings = await self._adapter_profiles(
                adapter=adapter,
                fund_name=fund_name,
                fetcher=fetcher,
                force=force,
            )

            warnings.extend(adapter_warnings)

            for profile_url in profile_urls:
                key = canonical_url(profile_url)

                if key in seen_urls:
                    continue

                seen_urls.add(key)

                candidates.append(
                    _profile_candidate(
                        profile_url=profile_url,
                        manager_name=adapter.name,
                        rank=len(candidates),
                    )
                )

        for catalog in self.catalogs:
            entry, catalog_warnings = await self._catalog_profile(
                catalog=catalog,
                fund_name=fund_name,
                fetcher=fetcher,
                force=force,
            )

            warnings.extend(catalog_warnings)

            if entry is None:
                continue

            key = canonical_url(entry.url)

            if key in seen_urls:
                continue

            seen_urls.add(key)

            candidates.append(
                _profile_candidate(
                    profile_url=entry.url,
                    manager_name=catalog.name,
                    rank=len(candidates),
                    title=entry.text,
                )
            )

        return CandidateDiscovery(
            candidates=tuple(candidates),
            warnings=tuple(warnings),
        )

    async def _adapter_profiles(
        self,
        *,
        adapter: DomainAdapter,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        # The adapters expect a validated fund record. A fund without a
        # known website is represented by the site of the adapter itself.
        # A placeholder domain would make an adapter treat the fund as
        # externally hosted and discard its own valid profile URLs.
        try:
            fund = FundInput(
                name=fund_name,
                web=ADAPTER_HOME_URLS.get(
                    adapter.name,
                    DEFAULT_ADAPTER_HOME_URL,
                ),
            )
        except ValueError as exc:
            return ((), (f"Adapter fallback could not build a fund record: {exc}",))

        try:
            result = await adapter.discover(
                fund=fund,
                fetcher=fetcher,
                force=force,
            )
        except FetchError as exc:
            return ((), (f"Adapter {adapter.name} failed: {exc.code}: {exc}",))
        except (UnicodeDecodeError, ValueError, RuntimeError) as exc:
            # Some manager catalogs are not served as UTF-8, which makes
            # the HTML parser fail while reading attributes. One broken
            # catalog must not fail the whole fund.
            return (
                (),
                (
                    f"Adapter {adapter.name} could not parse its catalog: "
                    f"{type(exc).__name__}: {exc}",
                ),
            )

        return (
            result.navigation_urls,
            result.warnings,
        )

    async def _catalog_profile(
        self,
        *,
        catalog: ManagerCatalog,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool,
    ) -> tuple[CatalogEntry | None, tuple[str, ...]]:
        entries, warnings = await self._catalog_entries_cached(
            catalog=catalog,
            fetcher=fetcher,
            force=force,
        )

        matched = match_catalog_entry(
            entries=entries,
            fund_name=fund_name,
            minimum_score=self.minimum_score,
        )

        return (
            matched,
            warnings,
        )

    async def _catalog_entries_cached(
        self,
        *,
        catalog: ManagerCatalog,
        fetcher: HttpFetcher,
        force: bool,
    ) -> tuple[tuple[CatalogEntry, ...], tuple[str, ...]]:
        """Download and parse one manager catalog at most once per run."""

        lock = self._catalog_locks.setdefault(
            catalog.name,
            asyncio.Lock(),
        )

        async with lock:
            cached_entries = self._catalog_entries.get(catalog.name)

            if cached_entries is not None and not force:
                return (
                    cached_entries,
                    self._catalog_warnings.get(catalog.name, ()),
                )

            warnings: list[str] = []

            entries: list[CatalogEntry] = []

            for catalog_url in catalog.catalog_urls:
                try:
                    result = await fetcher.fetch(
                        catalog_url,
                        force=force,
                    )
                except FetchError as exc:
                    warnings.append(
                        f"Manager catalog {catalog.name} could not be downloaded: {exc.code}: {exc}"
                    )

                    continue

                if not is_html_response(
                    content_type=result.content_type,
                    body=result.body,
                ):
                    warnings.append(f"Manager catalog {catalog.name} did not return an HTML page.")

                    continue

                try:
                    entries.extend(
                        parse_manager_catalog(
                            body=result.body,
                            page_url=result.final_url,
                            catalog=catalog,
                        )
                    )
                except (UnicodeDecodeError, ValueError, RuntimeError) as exc:
                    warnings.append(
                        f"Manager catalog {catalog.name} could not be parsed: "
                        f"{type(exc).__name__}: {exc}"
                    )

            self._catalog_entries[catalog.name] = tuple(entries)

            self._catalog_warnings[catalog.name] = tuple(warnings)

            return (
                tuple(entries),
                tuple(warnings),
            )


def parse_manager_catalog(
    *,
    body: bytes,
    page_url: str,
    catalog: ManagerCatalog,
) -> tuple[CatalogEntry, ...]:
    """Extract fund profile links from one manager catalog page."""

    if not body:
        return ()

    parser = LexborHTMLParser(decode_html_bytes(body))

    entries_by_url: dict[str, CatalogEntry] = {}

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

        if canonical_domain(resolved_url) != catalog.domain:
            continue

        normalized_url = canonical_url(resolved_url)

        lowered_url = normalized_url.casefold()

        if _has_excluded_path_segment(lowered_url):
            continue

        if lowered_url.rstrip("/") in {
            item.casefold().rstrip("/") for item in catalog.catalog_urls
        }:
            continue

        # A document link names a document, not a fund. Documents are
        # used only as proof that a section describes a real fund.
        if _is_document_link(
            url=lowered_url,
            node=node,
        ):
            continue

        text = _node_text(node)

        key = entity_key(text)

        if not key:
            continue

        _remember_entry(
            entries_by_url=entries_by_url,
            key_url=normalized_url,
            entry=CatalogEntry(
                url=resolved_url,
                text=text,
                key=key,
                tokens=frozenset(key.split()),
            ),
        )

    for entry in _parse_catalog_sections(
        parser=parser,
        page_url=page_url,
    ):
        # The raw URL is the key because canonical_url drops the fragment,
        # which is exactly what distinguishes one accordion section from
        # another on a single catalog page.
        _remember_entry(
            entries_by_url=entries_by_url,
            key_url=entry.url,
            entry=entry,
        )

    return tuple(sorted(entries_by_url.values(), key=lambda item: item.url))


# Titles of a fund block inside a catalog, in decreasing precision. They
# cover accordions, headings, table rows and definition-style listings.
SECTION_TITLE_SELECTORS: Final[tuple[str, ...]] = (
    ".m-toggle__title",
    "summary",
    "h1, h2, h3, h4, h5, h6",
    "label",
    "th",
    "td.first",
    "strong",
)


SECTION_SELECTORS: Final[str] = "div[id], section[id], article[id], li[id], details[id], tr[id]"


def _parse_catalog_sections(
    *,
    parser: LexborHTMLParser,
    page_url: str,
) -> tuple[CatalogEntry, ...]:
    """
    Extract funds that exist only as a section of the catalog page.

    Some administrators publish every fund as an accordion, a table block
    or a heading followed by its documents, without a detail page. The
    anchored section URL is then the most specific reference available.
    """

    entries: list[CatalogEntry] = []

    for node in parser.css(SECTION_SELECTORS):
        section_id = (node.attributes.get("id") or "").strip()

        if not section_id:
            continue

        if not _contains_document_link(node):
            continue

        title = _section_title(node)

        if not title:
            continue

        key = entity_key(title)

        if not key:
            continue

        entries.append(
            CatalogEntry(
                url=f"{page_url}#{section_id}",
                text=title,
                key=key,
                tokens=frozenset(key.split()),
            )
        )

    return tuple(entries)


def _section_title(
    node: Any,
) -> str:
    """Return the fund title of one catalog section."""

    for selector in SECTION_TITLE_SELECTORS:
        for title_node in node.css(selector):
            title = _node_text(title_node)

            if title and len(title) <= 200:
                return title

    return ""


DOCUMENT_URL_MARKERS: Final[tuple[str, ...]] = (
    ".pdf",
    ".doc",
    ".xls",
    "download",
    "dokument",
    "soubor",
    "/file",
)


def _is_document_link(
    *,
    url: str,
    node: Any,
) -> bool:
    """Return whether one link points at a document rather than a fund."""

    if any(marker in url for marker in DOCUMENT_URL_MARKERS):
        return True

    normalized_text = normalize_search_text(_node_text(node))

    return any(keyword in normalized_text for keyword in DOCUMENT_LINK_KEYWORDS)


def _contains_document_link(
    node: Any,
) -> bool:
    """Return whether a section links at least one document."""

    for link_node in node.css("a[href]"):
        raw_href = (link_node.attributes.get("href") or "").casefold()

        if not raw_href:
            continue

        if _is_document_link(
            url=raw_href,
            node=link_node,
        ):
            return True

    return False


DOCUMENT_LINK_KEYWORDS: Final[tuple[str, ...]] = (
    "statut",
    "kid",
    "klicove informace",
    "vyrocni zprava",
    "pololetni zprava",
    "ucetni zaverka",
    "factsheet",
    "informacni list",
    "memorandum",
    "sdeleni",
)


def _remember_entry(
    *,
    entries_by_url: dict[str, CatalogEntry],
    key_url: str,
    entry: CatalogEntry,
) -> None:
    current = entries_by_url.get(key_url)

    if current is None or len(entry.text) > len(current.text):
        entries_by_url[key_url] = entry


def match_catalog_entry(
    *,
    entries: tuple[CatalogEntry, ...],
    fund_name: str,
    minimum_score: float = 0.72,
) -> CatalogEntry | None:
    """Return only a confident catalog match for the exact fund name."""

    target_key = entity_key(fund_name)

    target_tokens = frozenset(target_key.split())

    if not target_key or not target_tokens:
        return None

    best_entry: CatalogEntry | None = None

    best_score = 0.0

    for entry in entries:
        score = catalog_match_score(
            target_key=target_key,
            target_tokens=target_tokens,
            candidate_key=entry.key,
            candidate_tokens=entry.tokens,
        )

        if score > best_score:
            best_entry = entry
            best_score = score

    required_score = 0.9 if len(target_tokens) == 1 else minimum_score

    if best_entry is None or best_score < required_score:
        return None

    return best_entry


def catalog_match_score(
    *,
    target_key: str,
    target_tokens: frozenset[str],
    candidate_key: str,
    candidate_tokens: frozenset[str],
) -> float:
    """Score one catalog entry against the requested fund name."""

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

    if target_coverage < 0.66:
        return 0.0

    containment_bonus = 0.16 if target_tokens <= candidate_tokens else 0.0

    return min(
        1.0,
        (target_coverage * 0.55)
        + (candidate_coverage * 0.15)
        + (sequence_ratio * 0.22)
        + containment_bonus,
    )


def entity_key(
    value: str,
) -> str:
    """Return distinctive fund name tokens joined into one match key."""

    normalized = normalize_search_text(value)

    tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    result: list[str] = []

    for token in tokens:
        if token in GENERIC_FUND_TOKENS or len(token) < 2 or token.isdigit():
            continue

        if token not in result:
            result.append(token)

    return " ".join(result)


def _profile_candidate(
    *,
    profile_url: str,
    manager_name: str,
    rank: int,
    title: str = "",
) -> DomainCandidate:
    domain = canonical_domain(profile_url)

    return DomainCandidate(
        domain=domain,
        url=f"https://{domain}/",
        source=CandidateSourceName.ADAPTER,
        rank=rank,
        evidence=(f"{manager_name} fund profile"),
        result_url=profile_url,
        title=title,
        engine=manager_name,
    )


def _has_excluded_path_segment(
    url: str,
) -> bool:
    """
    Detect non-fund pages by complete path segments.

    Substring matching is not usable here, because a legitimate fund slug
    such as "adax-fond-firemniho-nastupnictvi" contains "o-nas".
    """

    segments = [segment for segment in urlsplit(url).path.split("/") if segment]

    for segment in segments:
        for fragment in CATALOG_EXCLUDED_PATH_FRAGMENTS:
            if segment == fragment or segment.startswith(f"{fragment}-"):
                return True

    return False


def _node_text(
    node: Any,
) -> str:
    text_method = getattr(node, "text", None)

    if not callable(text_method):
        return ""

    text = text_method(
        separator=" ",
        strip=True,
    )

    return " ".join(str(text).split())
