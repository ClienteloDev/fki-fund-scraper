from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.domain_adapters.avant import AvantFundsAdapter
from fundscraper.domain_candidates import CandidateSourceName
from fundscraper.domain_manager_fallback import (
    ADAPTER_HOME_URLS,
    DEFAULT_ADAPTER_HOME_URL,
    CatalogEntry,
    ManagerAdapterFallback,
    ManagerCatalog,
    entity_key,
    match_catalog_entry,
    parse_manager_catalog,
)
from fundscraper.http_client import HttpFetcher

CODYA_CATALOG = ManagerCatalog(
    name="codya",
    domain="codyainvest.cz",
    catalog_urls=("https://www.codyainvest.cz/nase-fondy",),
)


CATALOG_PAGE = b"""
<html>
  <body>
    <a href="/nase-fondy/care-sicav-a-s">CARE SICAV, a.s.</a>

    <a href="/nase-fondy/adax-fond-firemniho-nastupnictvi">
      ADAX Fond firemniho nastupnictvi SICAV, a.s.
    </a>

    <a href="/kontakt">Kontakt</a>
  </body>
</html>
"""


PROFILE_PAGE = b"""
<html>
  <head>
    <title>ADAX Fond firemniho nastupnictvi SICAV, a.s. | CODYA invest</title>
  </head>
  <body>
    <h1>ADAX Fond firemniho nastupnictvi SICAV, a.s.</h1>

    <p>Fond kvalifikovanych investoru. Investicni strategie fondu.</p>

    <a href="/dokumenty/statut.pdf">Statut fondu</a>
  </body>
</html>
"""


def test_parses_fund_profile_links_from_a_catalog() -> None:
    entries = parse_manager_catalog(
        body=CATALOG_PAGE,
        page_url="https://www.codyainvest.cz/nase-fondy",
        catalog=CODYA_CATALOG,
    )

    urls = {entry.url for entry in entries}

    assert "https://www.codyainvest.cz/nase-fondy/adax-fond-firemniho-nastupnictvi" in urls
    assert "https://www.codyainvest.cz/nase-fondy/care-sicav-a-s" in urls

    # Non-fund pages are skipped.
    assert all("kontakt" not in url for url in urls)


def test_matches_only_the_requested_fund() -> None:
    entries = parse_manager_catalog(
        body=CATALOG_PAGE,
        page_url="https://www.codyainvest.cz/nase-fondy",
        catalog=CODYA_CATALOG,
    )

    matched = match_catalog_entry(
        entries=entries,
        fund_name="ADAX Fond firemního nástupnictví SICAV, a.s.",
    )

    assert matched is not None
    assert matched.url.endswith("/adax-fond-firemniho-nastupnictvi")

    assert (
        match_catalog_entry(
            entries=entries,
            fund_name="BRIXX SICAV, a.s.",
        )
        is None
    )


def test_generic_wording_is_removed_from_the_match_key() -> None:
    assert entity_key("ADAX Fond firemního nástupnictví SICAV, a.s.") == (
        "adax firemniho nastupnictvi"
    )

    assert (
        match_catalog_entry(
            entries=(
                CatalogEntry(
                    url="https://www.codyainvest.cz/nase-fondy/jiny",
                    text="Jiny fond SICAV",
                    key="jiny",
                    tokens=frozenset({"jiny"}),
                ),
            ),
            fund_name="ADAX Fond firemního nástupnictví SICAV, a.s.",
        )
        is None
    )


def test_returns_the_specific_fund_profile_url(
    tmp_path: Path,
) -> None:
    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        path = request.url.path

        if path == "/nase-fondy":
            content = CATALOG_PAGE
        elif path == "/nase-fondy/adax-fond-firemniho-nastupnictvi":
            content = PROFILE_PAGE
        else:
            return httpx.Response(
                status_code=404,
                request=request,
            )

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=content,
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
            discovery = await ManagerAdapterFallback(
                catalogs=(CODYA_CATALOG,),
                adapters=(),
            ).find(
                fund_name="ADAX Fond firemního nástupnictví SICAV, a.s.",
                fetcher=fetcher,
            )

        assert len(discovery.candidates) == 1

        candidate = discovery.candidates[0]

        assert candidate.source == CandidateSourceName.ADAPTER
        assert candidate.domain == "codyainvest.cz"

        # The specific fund profile, not only the manager homepage.
        assert candidate.result_url == (
            "https://www.codyainvest.cz/nase-fondy/adax-fond-firemniho-nastupnictvi"
        )

    asyncio.run(run_test())


def test_parses_a_catalog_that_is_not_served_as_utf8() -> None:
    legacy_catalog = (
        "<html><head>"
        '<meta http-equiv="content-type" content="text/html; charset=windows-1250">'
        "</head><body>"
        '<a href="/nase-fondy/adax-fond-firemniho-nastupnictvi" '
        'title="Fond firemního nástupnictví">'
        "ADAX Fond firemního nástupnictví SICAV, a.s."
        "</a>"
        "</body></html>"
    ).encode("cp1250")

    entries = parse_manager_catalog(
        body=legacy_catalog,
        page_url="https://www.codyainvest.cz/nase-fondy",
        catalog=CODYA_CATALOG,
    )

    assert len(entries) == 1

    assert entries[0].url.endswith("/adax-fond-firemniho-nastupnictvi")

    matched = match_catalog_entry(
        entries=entries,
        fund_name="ADAX Fond firemního nástupnictví SICAV, a.s.",
    )

    assert matched is not None


def test_catalog_is_downloaded_and_parsed_once_per_run(
    tmp_path: Path,
) -> None:
    catalog_requests = 0

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal catalog_requests

        if request.url.path == "/nase-fondy":
            catalog_requests += 1

            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "text/html",
                },
                content=CATALOG_PAGE,
                request=request,
            )

        return httpx.Response(
            status_code=404,
            request=request,
        )

    async def run_test() -> list[int]:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        fallback = ManagerAdapterFallback(
            catalogs=(CODYA_CATALOG,),
            adapters=(),
        )

        counts: list[int] = []

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            for fund_name in (
                "ADAX Fond firemního nástupnictví SICAV, a.s.",
                "CARE SICAV, a.s.",
                "Velaris Fond SICAV a.s.",
            ):
                discovery = await fallback.find(
                    fund_name=fund_name,
                    fetcher=fetcher,
                )

                counts.append(len(discovery.candidates))

        return counts

    counts = asyncio.run(run_test())

    # Two of the three funds are listed in the catalog.
    assert counts == [1, 1, 0]

    # The catalog was parsed once and reused for every fund.
    assert catalog_requests == 1


ACCORDION_CATALOG_PAGE = b"""
<html>
  <body>
    <div class="m-toggle" id="iromet_sicav_a_s_">
      <label class="m-toggle__label">
        <span class="m-toggle__title">IROMET SICAV a.s.</span>
      </label>
      <div class="m-toggle__content">
        <table class="full">
          <tbody>
            <tr>
              <td class="first">31.12.2025</td>
              <td><a href="download.php?id=3865">Vyrocni zprava 2025</a> (pdf)</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="m-toggle" id="asolero_sicav_a_s_">
      <label class="m-toggle__label">
        <span class="m-toggle__title">ASOLERO SICAV a.s.</span>
      </label>
      <div class="m-toggle__content">
        <table class="full">
          <tbody>
            <tr>
              <td class="first">31.12.2025</td>
              <td><a href="download.php?id=3866">Statut fondu</a> (pdf)</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="m-toggle" id="bez_dokumentu">
      <label class="m-toggle__label">
        <span class="m-toggle__title">PRAZDNY SICAV a.s.</span>
      </label>
    </div>
  </body>
</html>
"""


AMISTA_CATALOG = ManagerCatalog(
    name="amista",
    domain="amista.cz",
    catalog_urls=("https://www.amista.cz/povinne-informace.html",),
)


def test_parses_funds_listed_only_as_accordion_sections() -> None:
    entries = parse_manager_catalog(
        body=ACCORDION_CATALOG_PAGE,
        page_url="https://www.amista.cz/povinne-informace.html",
        catalog=AMISTA_CATALOG,
    )

    urls_by_key = {entry.key: entry.url for entry in entries}

    assert urls_by_key["iromet"] == (
        "https://www.amista.cz/povinne-informace.html#iromet_sicav_a_s_"
    )
    assert urls_by_key["asolero"] == (
        "https://www.amista.cz/povinne-informace.html#asolero_sicav_a_s_"
    )

    # A section without any document is not treated as a fund.
    assert "prazdny" not in urls_by_key

    # Document links are not offered as fund profiles.
    assert all("download.php" not in entry.url for entry in entries)


def test_matches_iromet_and_asolero_to_their_own_sections() -> None:
    entries = parse_manager_catalog(
        body=ACCORDION_CATALOG_PAGE,
        page_url="https://www.amista.cz/povinne-informace.html",
        catalog=AMISTA_CATALOG,
    )

    for fund_name, expected_fragment in (
        ("IROMET SICAV a.s.", "#iromet_sicav_a_s_"),
        ("ASOLERO SICAV, a.s.", "#asolero_sicav_a_s_"),
    ):
        matched = match_catalog_entry(
            entries=entries,
            fund_name=fund_name,
        )

        assert matched is not None
        assert matched.url.endswith(expected_fragment)


def test_adapters_receive_their_own_site_instead_of_a_placeholder() -> None:
    assert ADAPTER_HOME_URLS["avantfunds"] == "https://www.avantfunds.cz/"
    assert ADAPTER_HOME_URLS["amista"] == "https://www.amista.cz/"

    assert all("invalid" not in url for url in ADAPTER_HOME_URLS.values())
    assert "invalid" not in DEFAULT_ADAPTER_HOME_URL


def test_avant_profile_urls_are_not_discarded(
    tmp_path: Path,
) -> None:
    avant_catalog = b"""
    <html>
      <body>
        <section>
          <h3>SPILBERK investicni fond SICAV, a.s.</h3>
          <a href="/fondy/spilberk-investicni-fond-sicav-a-s/">Detail fondu</a>
          <a href="/dokumenty/spilberk-statut.pdf">Statut fondu</a>
        </section>
      </body>
    </html>
    """

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=avant_catalog,
            request=request,
        )

    async def run_test() -> tuple[str, ...]:
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
            discovery = await ManagerAdapterFallback(
                catalogs=(),
                adapters=(AvantFundsAdapter(),),
            ).find(
                fund_name="SPILBERK investiční fond SICAV, a.s.",
                fetcher=fetcher,
            )

        return tuple(candidate.result_url or "" for candidate in discovery.candidates)

    profile_urls = asyncio.run(run_test())

    assert any("/fondy/spilberk-investicni-fond-sicav-a-s/" in url for url in profile_urls)


def test_unknown_fund_produces_no_adapter_candidate(
    tmp_path: Path,
) -> None:
    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        if request.url.path == "/nase-fondy":
            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "text/html",
                },
                content=CATALOG_PAGE,
                request=request,
            )

        return httpx.Response(
            status_code=404,
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
            discovery = await ManagerAdapterFallback(
                catalogs=(CODYA_CATALOG,),
                adapters=(),
            ).find(
                fund_name="Velaris Fond SICAV a.s.",
                fetcher=fetcher,
            )

        assert discovery.candidates == ()

    asyncio.run(run_test())
