from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, quote_plus, urlsplit

from dotenv import load_dotenv
from selectolax.lexbor import LexborHTMLParser

from fundscraper.html_discovery import (
    is_html_response,
    normalize_search_text,
    resolve_link_url,
)
from fundscraper.http_client import (
    FetchError,
    HttpFetcher,
)
from fundscraper.normalization import canonical_domain, canonical_url

# A realistic browser user agent. Search engines and a noticeable part of
# fund websites answer an obvious crawler user agent with HTTP 403.
BROWSER_USER_AGENT: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class SearchEngine:
    """One server-rendered search endpoint usable without an API key."""

    name: str
    url_template: str
    own_domain: str


# Keyless, server-rendered endpoints, most permissive first. All of them
# apply anti-bot protection and may answer with an interstitial page
# instead of results, which is why several are attempted and why a real
# search API is preferred when its credentials are configured.
SEARCH_ENGINES: Final[tuple[SearchEngine, ...]] = (
    SearchEngine(
        name="mojeek",
        url_template="https://www.mojeek.com/search?q={query}",
        own_domain="mojeek.com",
    ),
    SearchEngine(
        name="duckduckgo-lite",
        url_template="https://lite.duckduckgo.com/lite/?q={query}",
        own_domain="duckduckgo.com",
    ),
    SearchEngine(
        name="duckduckgo-html",
        url_template="https://html.duckduckgo.com/html/?q={query}",
        own_domain="duckduckgo.com",
    ),
)


# Optional real search API. It is used automatically when both values are
# present in the environment or in the .env file, and it is the only
# reliable way to make search the primary discovery method.
SEARCH_API_KEY_VARIABLE: Final = "GOOGLE_SEARCH_API_KEY"

SEARCH_API_ENGINE_ID_VARIABLE: Final = "GOOGLE_SEARCH_ENGINE_ID"

SEARCH_API_URL: Final = (
    "https://www.googleapis.com/customsearch/v1?key={key}&cx={engine_id}&q={query}&num=10"
)


# Terms appended to the normalized fund name to reach fund presentation
# pages instead of registry and news mentions.
SEARCH_QUERY_TERMS: Final[tuple[str, ...]] = (
    "fond",
    "fund",
    "SICAV",
    "KID statut",
)


# Legal form and fund vocabulary removed from the searched name. The exact
# legal name is still searched separately as the first query.
LEGAL_SUFFIX_PATTERN: Final = re.compile(
    r"""
    (?:
        ,?\s*
        (?:
            a\.\s*s\.?
            |
            s\.\s*r\.\s*o\.?
            |
            akciov[aá]\s+spole[cč]nost
            |
            investi[cč]n[ií]\s+fond
            |
            podfond
            |
            SICAV
            |
            SIF
        )
        \b
        \.?
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.UNICODE,
)


HEURISTIC_TOP_LEVEL_DOMAINS: Final[tuple[str, ...]] = (
    ".cz",
    ".eu",
    ".com",
)


HEURISTIC_SUFFIXES: Final[tuple[str, ...]] = (
    "",
    "fond",
    "invest",
    "funds",
)


# Portals, registries, media, social networks, search engines, domain
# marketplaces and parking providers. They may describe a fund correctly,
# but they are never its official website.
EXCLUDED_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "above.com",
        "afternic.com",
        "akatcr.cz",
        "ares.gov.cz",
        "bing.com",
        "bisnode.cz",
        "bodis.com",
        "businessinfo.cz",
        "buttondown.email",
        "cnb.cz",
        "dan.com",
        "detail.cz",
        "docplayer.cz",
        "domainmarket.com",
        "domainseller.site",
        "duckduckgo.com",
        "e15.cz",
        "ecosia.org",
        "edb.cz",
        "emis.com",
        "emis.cz",
        "facebook.com",
        "finance.cz",
        "finmag.cz",
        "firmy.cz",
        "fki-fondy.cz",
        "github.com",
        "google.com",
        "hlidacstatu.cz",
        "hugedomains.com",
        "idnes.cz",
        "ihned.cz",
        "instagram.com",
        "issuu.com",
        "justice.cz",
        "kurzy.cz",
        "leiscan.com",
        "linkedin.com",
        "marginalia-search.com",
        "mastodon.social",
        "medium.com",
        "merk.cz",
        "mesec.cz",
        "mojeek.com",
        "novinky.cz",
        "opencorporates.com",
        "orbis.bvdinfo.com",
        "parkingcrew.net",
        "patria.cz",
        "penize.cz",
        "podnikatel.cz",
        "porovnejfondy.cz",
        "reddit.com",
        "rejstrik-firem.kurzy.cz",
        "scribd.com",
        "sedo.com",
        "sedoparking.com",
        "seznam.cz",
        "slideshare.net",
        "startmail.com",
        "startpage.com",
        "startupjobs.cz",
        "substack.com",
        "t.co",
        "torproject.org",
        "twitter.com",
        "undeveloped.com",
        "wikipedia.org",
        "x.com",
        "yahoo.com",
        "youtube.com",
        "yumpu.com",
        "zivefirmy.cz",
    }
)


# Hostname fragments used by domain parking and resale services.
PARKED_DOMAIN_FRAGMENTS: Final[tuple[str, ...]] = (
    "domainseller",
    "domainmarket",
    "domainparking",
    "parkingcrew",
    "sedoparking",
    "hugedomains",
    "afternic",
    "undeveloped",
    "bodis",
)


class CandidateSourceName:
    SEARCH = "search"
    HEURISTIC = "heuristic"
    ADAPTER = "adapter"


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One preserved search result before it becomes a candidate."""

    url: str
    domain: str
    title: str
    snippet: str
    query: str
    engine: str
    rank: int


@dataclass(frozen=True, slots=True)
class DomainCandidate:
    """
    One candidate official website proposed for a fund.

    ``url`` is the domain main page. ``result_url`` keeps the exact page
    returned by search or by a manager adapter, which is verified first
    because it usually names the fund far more precisely than the
    homepage of a manager or administrator.
    """

    domain: str
    url: str
    source: str
    rank: int
    evidence: str
    result_url: str | None = None
    title: str = ""
    snippet: str = ""
    query: str = ""
    engine: str = ""


@dataclass(frozen=True, slots=True)
class CandidateDiscovery:
    """Candidates proposed for one fund together with soft failures."""

    candidates: tuple[DomainCandidate, ...]
    warnings: tuple[str, ...]


# One domain frequently exposes a generic landing page and the exact fund
# profile. Keeping a few results per domain stops the generic page from
# hiding the profile, without flooding verification with one whole site.
MAXIMUM_RESULTS_PER_DOMAIN: Final = 3


@dataclass
class ResultSelection:
    """
    Deduplicate results by exact URL with a small per-domain allowance.

    Deduplicating by domain alone loses the fund profile whenever a
    generic page of the same domain was returned first.
    """

    maximum_per_domain: int = MAXIMUM_RESULTS_PER_DOMAIN

    seen_urls: set[str] = field(default_factory=set)

    domain_counts: dict[str, int] = field(default_factory=dict)

    def accept(
        self,
        *,
        url: str,
        domain: str,
    ) -> bool:
        key = canonical_url(url)

        if key in self.seen_urls:
            return False

        if self.domain_counts.get(domain, 0) >= self.maximum_per_domain:
            return False

        self.seen_urls.add(key)

        self.domain_counts[domain] = self.domain_counts.get(domain, 0) + 1

        return True


class CandidateSource(Protocol):
    """Interface implemented by lightweight candidate domain providers."""

    name: str

    async def find(
        self,
        *,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> CandidateDiscovery:
        """Propose candidate domains for one exact fund name."""


def strip_legal_suffixes(
    fund_name: str,
) -> str:
    """Remove legal form and generic fund wording from a fund name."""

    without_suffixes = LEGAL_SUFFIX_PATTERN.sub(
        " ",
        fund_name,
    )

    cleaned = re.sub(
        r"[,;]+",
        " ",
        without_suffixes,
    )

    return " ".join(cleaned.split()).strip()


def distinctive_name(
    fund_name: str,
) -> str:
    """Return the fund name reduced to its distinctive words."""

    distinctive = set(distinctive_name_tokens(fund_name))

    if not distinctive:
        return strip_legal_suffixes(fund_name)

    words = [
        word
        for word in strip_legal_suffixes(fund_name).split()
        if normalize_search_text(re.sub(r"[^\w]", "", word)) in distinctive
    ]

    return " ".join(words) or strip_legal_suffixes(fund_name)


def build_search_queries(
    fund_name: str,
    *,
    max_queries: int = 3,
) -> tuple[str, ...]:
    """
    Build ordered search queries for one exact fund name.

    The exact legal name is searched first, then the name without legal
    suffixes, then the distinctive part of the name combined with fund
    wording. The last group is what finds sites such as velarisfund.com
    for a fund named "Velaris Fond SICAV a.s.".
    """

    if max_queries < 1:
        return ()

    exact_name = " ".join(fund_name.split()).strip()

    normalized_name = strip_legal_suffixes(exact_name)

    distinctive = distinctive_name(exact_name)

    queries: list[str] = []

    for query in (
        f'"{exact_name}"',
        normalized_name,
        *(f"{distinctive} {term}" for term in SEARCH_QUERY_TERMS),
    ):
        candidate_query = query.strip()

        if not candidate_query or candidate_query in {'""', ""}:
            continue

        if candidate_query not in queries:
            queries.append(candidate_query)

        if len(queries) >= max_queries:
            break

    return tuple(queries)


def is_excluded_domain(
    domain: str,
) -> bool:
    """Return whether a domain is a known non-official or parked source."""

    normalized = domain.casefold().removeprefix("www.")

    if not normalized:
        return True

    if any(fragment in normalized for fragment in PARKED_DOMAIN_FRAGMENTS):
        return True

    for excluded in EXCLUDED_DOMAINS:
        if normalized == excluded or normalized.endswith(f".{excluded}"):
            return True

    return False


class WebSearch:
    """
    Propose candidate domains from real, keyless web search results.

    Several queries and several server-rendered engines are attempted so
    that one blocked or empty engine does not remove the primary
    discovery method. Search results are the preferred candidate source;
    generated name variations are only a fallback.
    """

    name = CandidateSourceName.SEARCH

    def __init__(
        self,
        *,
        max_results: int = 8,
        max_queries: int = 3,
        engines: tuple[SearchEngine, ...] = SEARCH_ENGINES,
    ) -> None:
        if max_results < 1:
            raise ValueError("max_results must be at least one")

        if max_queries < 1:
            raise ValueError("max_queries must be at least one")

        if not engines:
            raise ValueError("At least one search engine must be configured")

        self.max_results = max_results
        self.max_queries = max_queries
        self.engines = engines

    async def find(
        self,
        *,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> CandidateDiscovery:
        queries = build_search_queries(
            fund_name,
            max_queries=self.max_queries,
        )

        candidates: list[DomainCandidate] = []

        warnings: list[str] = []

        selection = ResultSelection()

        for query in queries:
            query_succeeded = False

            for engine in self.engines:
                try:
                    result = await fetcher.fetch(
                        engine.url_template.format(query=quote_plus(query)),
                        force=force,
                    )
                except FetchError as exc:
                    warnings.append(f"Search engine {engine.name} failed: {exc.code}: {exc}")

                    continue

                if not is_html_response(
                    content_type=result.content_type,
                    body=result.body,
                ):
                    warnings.append(f"Search engine {engine.name} did not return an HTML page.")

                    continue

                search_results = parse_search_results(
                    body=result.body,
                    page_url=result.final_url,
                    engine=engine,
                    query=query,
                )

                if not search_results:
                    warnings.append(f"Search engine {engine.name} returned no usable result links.")

                    continue

                query_succeeded = True

                for search_result in search_results:
                    if not selection.accept(
                        url=search_result.url,
                        domain=search_result.domain,
                    ):
                        continue

                    candidates.append(
                        _candidate_from_result(
                            search_result=search_result,
                            rank=len(candidates),
                        )
                    )

                break

            if query_succeeded and len(candidates) >= self.max_results:
                break

        return CandidateDiscovery(
            candidates=tuple(candidates[: self.max_results]),
            warnings=tuple(warnings),
        )


def parse_search_results(
    *,
    body: bytes,
    page_url: str,
    engine: SearchEngine,
    query: str = "",
) -> tuple[SearchResult, ...]:
    """
    Extract external search results with their URL, title and snippet.

    The exact result URL is preserved because it usually points directly
    at the fund page rather than at the homepage of its domain.
    """

    if not body:
        return ()

    parser = LexborHTMLParser(body)

    results: list[SearchResult] = []

    selection = ResultSelection()

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

        target_url = _unwrap_redirect_url(
            url=resolved_url,
            engine=engine,
        )

        if target_url is None:
            continue

        domain = canonical_domain(target_url)

        if not domain:
            continue

        if domain == engine.own_domain or domain.endswith(f".{engine.own_domain}"):
            continue

        if is_excluded_domain(domain):
            continue

        if not selection.accept(
            url=target_url,
            domain=domain,
        ):
            continue

        results.append(
            SearchResult(
                url=target_url,
                domain=domain,
                title=_node_text(node),
                snippet=_result_snippet(node),
                query=query,
                engine=engine.name,
                rank=len(results),
            )
        )

    return tuple(results)


def parse_search_result_domains(
    *,
    body: bytes,
    page_url: str,
    engine: SearchEngine,
) -> tuple[str, ...]:
    """Extract unique external result domains from a search result page."""

    return tuple(
        result.domain
        for result in parse_search_results(
            body=body,
            page_url=page_url,
            engine=engine,
        )
    )


class SearchApi:
    """
    Propose candidate domains from a real search API.

    Every keyless HTML search endpoint applies anti-bot protection and
    regularly answers with an interstitial page instead of results. When
    API credentials are configured, this source makes search genuinely
    reliable as the primary discovery method.
    """

    name = CandidateSourceName.SEARCH

    def __init__(
        self,
        *,
        api_key: str,
        engine_id: str,
        max_results: int = 8,
        max_queries: int = 3,
        url_template: str = SEARCH_API_URL,
    ) -> None:
        if not api_key or not engine_id:
            raise ValueError("Search API key and engine identifier are required")

        self.api_key = api_key
        self.engine_id = engine_id
        self.max_results = max_results
        self.max_queries = max_queries
        self.url_template = url_template

    @classmethod
    def from_environment(
        cls,
        *,
        max_results: int = 8,
        max_queries: int = 3,
    ) -> SearchApi | None:
        """Build the API search source when credentials are configured."""

        load_dotenv()

        api_key = os.getenv(SEARCH_API_KEY_VARIABLE, "").strip()

        engine_id = os.getenv(SEARCH_API_ENGINE_ID_VARIABLE, "").strip()

        if not api_key or not engine_id:
            return None

        return cls(
            api_key=api_key,
            engine_id=engine_id,
            max_results=max_results,
            max_queries=max_queries,
        )

    async def find(
        self,
        *,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> CandidateDiscovery:
        queries = build_search_queries(
            fund_name,
            max_queries=self.max_queries,
        )

        candidates: list[DomainCandidate] = []

        warnings: list[str] = []

        selection = ResultSelection()

        for query in queries:
            request_url = self.url_template.format(
                key=quote_plus(self.api_key),
                engine_id=quote_plus(self.engine_id),
                query=quote_plus(query),
            )

            try:
                result = await fetcher.fetch(
                    request_url,
                    force=force,
                )
            except FetchError as exc:
                warnings.append(f"Search API request failed: {exc.code}: {exc}")

                continue

            for search_result in parse_search_api_results(
                body=result.body,
                query=query,
            ):
                if not selection.accept(
                    url=search_result.url,
                    domain=search_result.domain,
                ):
                    continue

                candidates.append(
                    _candidate_from_result(
                        search_result=search_result,
                        rank=len(candidates),
                    )
                )

            if len(candidates) >= self.max_results:
                break

        return CandidateDiscovery(
            candidates=tuple(candidates[: self.max_results]),
            warnings=tuple(warnings),
        )


def parse_search_api_results(
    *,
    body: bytes,
    query: str = "",
) -> tuple[SearchResult, ...]:
    """Extract results with URL, title and snippet from a JSON API response."""

    try:
        payload: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ()

    if not isinstance(payload, dict):
        return ()

    items = payload.get("items")

    if not isinstance(items, list):
        return ()

    results: list[SearchResult] = []

    selection = ResultSelection()

    for item in items:
        if not isinstance(item, dict):
            continue

        link = item.get("link")

        if not isinstance(link, str):
            continue

        domain = canonical_domain(link)

        if not domain:
            continue

        if is_excluded_domain(domain):
            continue

        if not selection.accept(
            url=link,
            domain=domain,
        ):
            continue

        results.append(
            SearchResult(
                url=link,
                domain=domain,
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
                query=query,
                engine="search-api",
                rank=len(results),
            )
        )

    return tuple(results)


def parse_search_api_domains(
    body: bytes,
) -> tuple[str, ...]:
    """Extract result domains from a JSON search API response."""

    return tuple(result.domain for result in parse_search_api_results(body=body))


def _candidate_from_result(
    *,
    search_result: SearchResult,
    rank: int,
) -> DomainCandidate:
    return DomainCandidate(
        domain=search_result.domain,
        url=f"https://{search_result.domain}/",
        source=CandidateSourceName.SEARCH,
        rank=rank,
        evidence=(f"{search_result.engine} result for query: {search_result.query}"),
        result_url=search_result.url,
        title=search_result.title,
        snippet=search_result.snippet,
        query=search_result.query,
        engine=search_result.engine,
    )


def _result_snippet(
    node: Any,
) -> str:
    """Return the surrounding text of one search result link."""

    current: Any | None = node

    for _ in range(3):
        if current is None:
            break

        current = current.parent

        if current is None:
            break

        text = _node_text(current)

        if len(text) >= 60:
            return text[:400]

    return ""


def _node_text(
    node: Any,
) -> str:
    text = node.text(
        separator=" ",
        strip=True,
    )

    return " ".join(str(text).split())


class HeuristicDomainGuesser:
    """
    Propose domains built from significant tokens of the legal fund name.

    This is only a fallback for funds that real search does not cover.
    Every generated domain must still pass the main-page verification.
    """

    name = CandidateSourceName.HEURISTIC

    def __init__(
        self,
        *,
        max_results: int = 4,
    ) -> None:
        if max_results < 0:
            raise ValueError("max_results must not be negative")

        self.max_results = max_results

    async def find(
        self,
        *,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> CandidateDiscovery:
        # Generated domains need no network access. The arguments exist
        # only to satisfy the shared candidate source interface.
        del fetcher, force

        return CandidateDiscovery(
            candidates=guess_domains(
                fund_name=fund_name,
                max_results=self.max_results,
            ),
            warnings=(),
        )


def guess_domains(
    *,
    fund_name: str,
    max_results: int,
) -> tuple[DomainCandidate, ...]:
    """Build heuristic hostname candidates from distinctive name tokens."""

    if max_results < 1:
        return ()

    tokens = distinctive_name_tokens(fund_name)

    if not tokens:
        return ()

    bases: list[str] = []

    for base in (
        tokens[0],
        "".join(tokens[:2]),
        "".join(tokens),
    ):
        if base and base not in bases:
            bases.append(base)

    candidates: list[DomainCandidate] = []

    seen_domains: set[str] = set()

    for suffix in HEURISTIC_SUFFIXES:
        for base in bases:
            for top_level_domain in HEURISTIC_TOP_LEVEL_DOMAINS:
                if len(candidates) >= max_results:
                    return tuple(candidates)

                domain = f"{base}{suffix}{top_level_domain}"

                if domain in seen_domains or is_excluded_domain(domain):
                    continue

                seen_domains.add(domain)

                candidates.append(
                    DomainCandidate(
                        domain=domain,
                        url=f"https://{domain}/",
                        source=CandidateSourceName.HEURISTIC,
                        rank=len(candidates),
                        evidence=(f"Generated from fund name tokens: {' '.join(tokens)}"),
                    )
                )

    return tuple(candidates)


async def discover_domain_candidates(
    *,
    fund_name: str,
    fetcher: HttpFetcher,
    sources: tuple[CandidateSource, ...],
    fallback_sources: tuple[CandidateSource, ...] = (),
    max_candidates: int = 8,
    minimum_primary_candidates: int = 2,
    force: bool = False,
) -> CandidateDiscovery:
    """
    Collect candidate domains, preferring real search results.

    Fallback sources are only consulted when the primary sources did not
    return enough candidates for this fund.
    """

    if max_candidates < 1:
        raise ValueError("max_candidates must be at least one")

    candidates: list[DomainCandidate] = []

    warnings: list[str] = []

    selection = ResultSelection()

    async def collect(
        active_sources: tuple[CandidateSource, ...],
        *,
        stop_at: int,
    ) -> None:
        for source in active_sources:
            if len(candidates) >= stop_at:
                return

            discovery = await source.find(
                fund_name=fund_name,
                fetcher=fetcher,
                force=force,
            )

            warnings.extend(discovery.warnings)

            for candidate in discovery.candidates:
                if not selection.accept(
                    url=(candidate.result_url or candidate.url),
                    domain=candidate.domain,
                ):
                    continue

                candidates.append(candidate)

                if len(candidates) >= max_candidates:
                    return

    # Once one primary source produced enough candidates, the remaining
    # primary sources are skipped. That avoids requesting rate limited
    # search engines when a reliable source already answered.
    await collect(
        sources,
        stop_at=max(
            minimum_primary_candidates,
            1,
        ),
    )

    if len(candidates) < minimum_primary_candidates and fallback_sources:
        await collect(
            fallback_sources,
            stop_at=max_candidates,
        )

    return CandidateDiscovery(
        candidates=tuple(_rank_candidates(fund_name=fund_name, candidates=candidates)),
        warnings=tuple(warnings),
    )


def default_candidate_sources(
    *,
    search_results: int = 8,
    search_queries: int = 3,
) -> tuple[CandidateSource, ...]:
    """
    Return the primary, search-based candidate source configuration.

    A configured search API is preferred. The keyless HTML engines remain
    enabled after it because they still resolve part of the funds when
    they are not currently rate limited.
    """

    if search_results < 1:
        return ()

    sources: list[CandidateSource] = []

    api_search = SearchApi.from_environment(
        max_results=search_results,
        max_queries=search_queries,
    )

    if api_search is not None:
        sources.append(api_search)

    sources.append(
        WebSearch(
            max_results=search_results,
            max_queries=search_queries,
        )
    )

    return tuple(sources)


def default_fallback_sources(
    *,
    heuristic_results: int = 4,
) -> tuple[CandidateSource, ...]:
    """Return the generated-domain fallback source configuration."""

    if heuristic_results < 1:
        return ()

    return (HeuristicDomainGuesser(max_results=heuristic_results),)


# Legal form, fund vocabulary and other terms that do not identify one
# exact fund. They are still used as weak context, never as evidence.
GENERIC_NAME_TOKENS: Final[frozenset[str]] = frozenset(
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
        "invest",
        "investicni",
        "investment",
        "investments",
        "kapital",
        "kapitalem",
        "kvalifikovanych",
        "otevreny",
        "podfond",
        "promenneho",
        "promennym",
        "promenny",
        "sicav",
        "sif",
        "spolecnost",
        "sro",
        "subfund",
        "usd",
        "uzavreny",
        "zakladni",
        "zakladniho",
        "zakladnim",
    }
)


def distinctive_name_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    """Return only tokens that actually identify one exact fund."""

    tokens = _name_tokens(fund_name)

    return tuple(token for token in tokens if token not in GENERIC_NAME_TOKENS)


def generic_name_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    """Return the low-weight legal and fund vocabulary tokens of a name."""

    tokens = _name_tokens(fund_name)

    return tuple(token for token in tokens if token in GENERIC_NAME_TOKENS)


def _name_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    normalized = normalize_search_text(fund_name)

    raw_tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    tokens: list[str] = []

    for token in raw_tokens:
        # Two-character tokens such as "JF" or a subfund numeral are real
        # parts of a fund name. Only the single letters of a legal form,
        # for example the "a" and "s" of "a.s.", are dropped.
        if len(token) < 2:
            continue

        if token not in tokens:
            tokens.append(token)

    return tuple(tokens)


# Fund wording inside a hostname. A domain combining a distinctive fund
# token with this wording, such as velarisfund.com, is a far better
# candidate than the bare distinctive token, such as velaris.com.
HOSTNAME_FUND_TOKENS: Final[tuple[str, ...]] = (
    "fund",
    "fonds",
    "fond",
    "invest",
    "sicav",
    "capital",
    "asset",
    "partners",
)


SOURCE_PRIORITY: Final[dict[str, int]] = {
    CandidateSourceName.SEARCH: 0,
    CandidateSourceName.ADAPTER: 1,
    CandidateSourceName.HEURISTIC: 2,
}


def hostname_preference(
    *,
    fund_name: str,
    domain: str,
) -> int:
    """Rank a hostname: 0 distinctive plus fund wording, 2 unrelated."""

    tokens = distinctive_name_tokens(fund_name)

    normalized_domain = normalize_search_text(domain)

    if not any(token in normalized_domain for token in tokens):
        return 2

    if any(keyword in normalized_domain for keyword in HOSTNAME_FUND_TOKENS):
        return 0

    return 1


def _rank_candidates(
    *,
    fund_name: str,
    candidates: list[DomainCandidate],
) -> list[DomainCandidate]:
    """Prefer search results and fund-like hostnames of the exact fund."""

    def sort_key(
        item: tuple[int, DomainCandidate],
    ) -> tuple[int, int, int]:
        position, candidate = item

        return (
            SOURCE_PRIORITY.get(candidate.source, 3),
            hostname_preference(
                fund_name=fund_name,
                domain=candidate.domain,
            ),
            position,
        )

    ordered = sorted(
        enumerate(candidates),
        key=sort_key,
    )

    return [
        replace(
            candidate,
            rank=rank,
        )
        for rank, (_, candidate) in enumerate(ordered)
    ]


def _unwrap_redirect_url(
    *,
    url: str,
    engine: SearchEngine,
) -> str | None:
    """Return the real target of a search engine redirect link."""

    parsed = urlsplit(url)

    host = (parsed.hostname or "").casefold().removeprefix("www.")

    if host != engine.own_domain and not host.endswith(f".{engine.own_domain}"):
        return url

    target_values = parse_qs(parsed.query).get("uddg")

    if not target_values:
        return None

    target_url = target_values[0].strip()

    if not target_url.casefold().startswith(
        (
            "http://",
            "https://",
        )
    ):
        return None

    return target_url
