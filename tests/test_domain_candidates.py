from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.domain_candidates import (
    MAXIMUM_RESULTS_PER_DOMAIN,
    SEARCH_ENGINES,
    CandidateSourceName,
    HeuristicDomainGuesser,
    SearchApi,
    WebSearch,
    build_search_queries,
    discover_domain_candidates,
    distinctive_name_tokens,
    generic_name_tokens,
    guess_domains,
    hostname_preference,
    is_excluded_domain,
    parse_search_api_results,
    parse_search_result_domains,
    parse_search_results,
    strip_legal_suffixes,
)
from fundscraper.http_client import HttpFetcher

FUND_NAME = "ADAX Fond firemního nástupnictví SICAV, a.s."


SEARCH_RESULT_PAGE = b"""
<html>
  <body>
    <a href="/settings">Settings</a>

    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.adaxfond.cz%2F&amp;rut=1">
      ADAX Fond firemniho nastupnictvi
    </a>

    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcs.wikipedia.org%2Fwiki%2FAdax&amp;rut=2">
      Wikipedia
    </a>

    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.hugedomains.com%2Fadax&amp;rut=3">
      Domain for sale
    </a>

    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.avantfunds.cz%2Ffondy%2Fadax&amp;rut=4">
      AVANT
    </a>
  </body>
</html>
"""


def test_strips_legal_suffixes_from_fund_name() -> None:
    assert strip_legal_suffixes(FUND_NAME) == "ADAX Fond firemního nástupnictví"

    assert strip_legal_suffixes("BRIXX SICAV, a.s.") == "BRIXX"

    assert strip_legal_suffixes("ABC investiční fond s.r.o.") == "ABC"


def test_builds_ordered_search_queries() -> None:
    queries = build_search_queries(
        FUND_NAME,
        max_queries=4,
    )

    assert queries[0] == f'"{FUND_NAME}"'
    assert queries[1] == "ADAX Fond firemního nástupnictví"

    # The distinctive part of the name combined with fund wording.
    assert queries[2] == "ADAX firemního nástupnictví fond"
    assert queries[3] == "ADAX firemního nástupnictví fund"


def test_search_queries_combine_distinctive_name_with_fund_terms() -> None:
    queries = build_search_queries(
        "Velaris Fond SICAV a.s.",
        max_queries=5,
    )

    assert queries[0] == '"Velaris Fond SICAV a.s."'
    assert "Velaris fund" in queries
    assert "Velaris fond" in queries


def test_prefers_fund_hostname_over_bare_distinctive_token() -> None:
    fund_name = "Velaris Fond SICAV a.s."

    assert (
        hostname_preference(
            fund_name=fund_name,
            domain="velarisfund.com",
        )
        == 0
    )

    assert (
        hostname_preference(
            fund_name=fund_name,
            domain="velaris.com",
        )
        == 1
    )

    assert (
        hostname_preference(
            fund_name=fund_name,
            domain="unrelated.com",
        )
        == 2
    )


def test_weights_distinctive_tokens_over_fund_vocabulary() -> None:
    assert distinctive_name_tokens("AMBEAT INVEST SICAV, a.s.") == ("ambeat",)

    assert set(generic_name_tokens("AMBEAT INVEST SICAV, a.s.")) == {
        "invest",
        "sicav",
    }

    assert distinctive_name_tokens(FUND_NAME) == (
        "adax",
        "firemniho",
        "nastupnictvi",
    )


def test_excludes_portals_marketplaces_and_social_networks() -> None:
    assert is_excluded_domain("porovnejfondy.cz")
    assert is_excluded_domain("cs.wikipedia.org")
    assert is_excluded_domain("www.kurzy.cz")
    assert is_excluded_domain("or.justice.cz")
    assert is_excluded_domain("hugedomains.com")

    # Third-party company and fund databases are never an official site.
    assert is_excluded_domain("emis.com")
    assert is_excluded_domain("www.emis.com")
    assert is_excluded_domain("opencorporates.com")
    assert is_excluded_domain("bisnode.cz")
    assert is_excluded_domain("trusted.domainseller.site")
    assert is_excluded_domain("sedoparking.com")

    assert not is_excluded_domain("adaxfond.cz")


def test_parses_external_result_domains_only() -> None:
    duckduckgo_engine = next(
        engine for engine in SEARCH_ENGINES if engine.own_domain == "duckduckgo.com"
    )

    domains = parse_search_result_domains(
        body=SEARCH_RESULT_PAGE,
        page_url="https://lite.duckduckgo.com/lite/?q=adax",
        engine=duckduckgo_engine,
    )

    assert domains == (
        "adaxfond.cz",
        "avantfunds.cz",
    )


def test_search_is_the_primary_candidate_source(
    tmp_path: Path,
) -> None:
    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=SEARCH_RESULT_PAGE,
            request=request,
        )

    async def run_test() -> None:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            discovery = await discover_domain_candidates(
                fund_name=FUND_NAME,
                fetcher=fetcher,
                sources=(WebSearch(),),
                fallback_sources=(HeuristicDomainGuesser(max_results=4),),
                max_candidates=8,
            )

        sources = {candidate.source for candidate in discovery.candidates}

        assert sources == {CandidateSourceName.SEARCH}

        # The hostname carrying a distinctive fund token is ranked first.
        assert discovery.candidates[0].domain == "adaxfond.cz"

    asyncio.run(run_test())


def test_falls_back_to_generated_domains_when_search_fails(
    tmp_path: Path,
) -> None:
    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            request=request,
        )

    async def run_test() -> None:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            discovery = await discover_domain_candidates(
                fund_name=FUND_NAME,
                fetcher=fetcher,
                sources=(WebSearch(),),
                fallback_sources=(HeuristicDomainGuesser(max_results=3),),
                max_candidates=8,
            )

        assert discovery.warnings

        assert {candidate.source for candidate in discovery.candidates} == {
            CandidateSourceName.HEURISTIC
        }

        assert discovery.candidates[0].domain == "adax.cz"

    asyncio.run(run_test())


def test_allows_only_a_small_number_of_results_per_domain() -> None:
    body = json.dumps(
        {
            "items": [
                {"link": f"https://www.adaxfond.cz/page-{index}"}
                for index in range(MAXIMUM_RESULTS_PER_DOMAIN + 3)
            ]
        }
    ).encode("utf-8")

    results = parse_search_api_results(body=body)

    assert len(results) == MAXIMUM_RESULTS_PER_DOMAIN


def test_parses_search_api_results_and_drops_excluded_domains() -> None:
    body = json.dumps(
        {
            "items": [
                {
                    "link": "https://www.adaxfond.cz/o-fondu",
                    "title": "ADAX o fondu",
                    "snippet": "Fond firemniho nastupnictvi",
                },
                {"link": "https://cs.wikipedia.org/wiki/Adax"},
                {"link": "https://www.adaxfond.cz/dokumenty"},
                {"link": "https://www.avantfunds.cz/fondy/adax"},
            ]
        }
    ).encode("utf-8")

    results = parse_search_api_results(
        body=body,
        query="adax fond",
    )

    # Deduplication is by exact URL, so a second page of the same domain
    # is kept and cannot be hidden by a generic first result.
    assert [result.url for result in results] == [
        "https://www.adaxfond.cz/o-fondu",
        "https://www.adaxfond.cz/dokumenty",
        "https://www.avantfunds.cz/fondy/adax",
    ]

    # The exact result URL, title, snippet, query and rank are preserved.
    assert results[0].url == "https://www.adaxfond.cz/o-fondu"
    assert results[0].title == "ADAX o fondu"
    assert results[0].snippet == "Fond firemniho nastupnictvi"
    assert results[0].query == "adax fond"
    assert results[0].rank == 0

    assert parse_search_api_results(body=b"not json") == ()


def test_search_api_is_used_before_keyless_engines(
    tmp_path: Path,
) -> None:
    requested_urls: list[str] = []

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        requested_urls.append(str(request.url))

        if request.url.host == "www.googleapis.com":
            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "application/json",
                },
                content=json.dumps(
                    {"items": [{"link": "https://www.adaxfond.cz/"}]},
                ).encode("utf-8"),
                request=request,
            )

        return httpx.Response(
            status_code=403,
            request=request,
        )

    async def run_test() -> None:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            discovery = await discover_domain_candidates(
                fund_name=FUND_NAME,
                fetcher=fetcher,
                sources=(
                    SearchApi(
                        api_key="test-key",
                        engine_id="test-engine",
                        max_results=4,
                        max_queries=1,
                    ),
                    WebSearch(),
                ),
                fallback_sources=(HeuristicDomainGuesser(max_results=4),),
                max_candidates=8,
                minimum_primary_candidates=1,
            )

        assert [candidate.domain for candidate in discovery.candidates] == ["adaxfond.cz"]

        # The exact result URL is kept next to the domain main page.
        assert discovery.candidates[0].result_url == "https://www.adaxfond.cz/"
        assert discovery.candidates[0].url == "https://adaxfond.cz/"
        assert discovery.candidates[0].query

        # The rate limited keyless engines are not requested at all.
        assert all("duckduckgo" not in url and "mojeek" not in url for url in requested_urls)

    asyncio.run(run_test())


def test_html_results_preserve_url_title_and_rank() -> None:
    duckduckgo_engine = next(
        engine for engine in SEARCH_ENGINES if engine.own_domain == "duckduckgo.com"
    )

    results = parse_search_results(
        body=SEARCH_RESULT_PAGE,
        page_url="https://lite.duckduckgo.com/lite/?q=adax",
        engine=duckduckgo_engine,
        query="adax",
    )

    assert [result.domain for result in results] == [
        "adaxfond.cz",
        "avantfunds.cz",
    ]

    assert results[0].url == "https://www.adaxfond.cz/"
    assert "ADAX" in results[0].title
    assert results[0].query == "adax"
    assert results[0].rank == 0
    assert results[1].rank == 1


def test_generates_fallback_domains_from_distinctive_tokens() -> None:
    candidates = guess_domains(
        fund_name=FUND_NAME,
        max_results=4,
    )

    domains = [candidate.domain for candidate in candidates]

    assert domains[0] == "adax.cz"
    assert len(domains) == len(set(domains)) == 4
