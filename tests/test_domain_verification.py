from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.domain_candidates import CandidateSourceName, DomainCandidate
from fundscraper.domain_verification import (
    MAXIMUM_VERIFIED_TEXT_CHARACTERS,
    DomainVerificationCache,
    MatchLocation,
    PageContent,
    VerificationResult,
    evaluate_document_signals,
    evaluate_name_signals,
    extract_page_content,
    is_parked_page,
    name_phrases,
    verify_candidate,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.output_models import DocumentType

FUND_NAME = "ADAX Fond firemního nástupnictví SICAV, a.s."

AMBEAT_NAME = "AMBEAT INVEST SICAV, a.s."


OFFICIAL_PAGE_WITH_DOCUMENTS = b"""
<html>
  <head>
    <title>ADAX Fond firemniho nastupnictvi SICAV, a.s.</title>
  </head>
  <body>
    <h1>ADAX Fond firemniho nastupnictvi</h1>

    <a href="/dokumenty/statut.pdf">Statut fondu</a>

    <a href="/dokumenty/kid.pdf">Sdeleni klicovych informaci</a>
  </body>
</html>
"""


# A real fund website whose homepage links no document at all.
OFFICIAL_PAGE_WITHOUT_DOCUMENTS = b"""
<html>
  <head>
    <title>AMBEAT INVEST</title>
    <meta property="og:site_name" content="AMBEAT INVEST SICAV" />
  </head>
  <body>
    <h1>AMBEAT INVEST SICAV, a.s.</h1>

    <p>
      Fond kvalifikovanych investoru se zamerenim na zdravotnictvi.
      Investicni strategie fondu cili na dlouhodobe zhodnoceni.
    </p>

    <a href="/kontakt">Kontakt</a>
  </body>
</html>
"""


# The page of a fund administrator that names the fund only in body text.
ADMINISTRATOR_PAGE = b"""
<html>
  <head>
    <title>Cody Invest</title>
  </head>
  <body>
    <h1>Nase fondy</h1>

    <p>
      Spravujeme fondy kvalifikovanych investoru, mimo jine
      ADAX Fond firemniho nastupnictvi SICAV, a.s.
    </p>

    <a href="/dokumenty">Dokumenty</a>
  </body>
</html>
"""


# An unrelated company that happens to share a distinctive name token.
UNRELATED_COMPANY_PAGE = b"""
<html>
  <head>
    <title>Adax - vyroba elektrickych topidel</title>
  </head>
  <body>
    <h1>Adax topidla</h1>

    <p>Vyrabime elektricka topidla od roku 1948.</p>

    <a href="/kontakt">Kontakt</a>
  </body>
</html>
"""


PARKED_PAGE = b"""
<html>
  <head>
    <title>adax.eu</title>
  </head>
  <body>
    <h1>This domain is for sale</h1>

    <p>Buy this domain today.</p>
  </body>
</html>
"""


def candidate_for(
    domain: str,
    *,
    source: str = CandidateSourceName.SEARCH,
) -> DomainCandidate:
    return DomainCandidate(
        domain=domain,
        url=f"https://{domain}/",
        source=source,
        rank=0,
        evidence="test",
    )


def verify(
    *,
    tmp_path: Path,
    fund_name: str,
    domain: str,
    pages: dict[str, bytes],
    source: str = CandidateSourceName.SEARCH,
) -> VerificationResult:
    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        host = (request.url.host or "").removeprefix("www.")

        body = pages.get(host)

        if body is None:
            return httpx.Response(
                status_code=404,
                request=request,
            )

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=body,
            request=request,
        )

    async def run_test() -> VerificationResult:
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
            return await verify_candidate(
                fund_name=fund_name,
                candidate=candidate_for(
                    domain,
                    source=source,
                ),
                fetcher=fetcher,
            )

    return asyncio.run(run_test())


def test_accepts_page_naming_the_fund_and_linking_documents(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="adaxfond.cz",
        pages={"adaxfond.cz": OFFICIAL_PAGE_WITH_DOCUMENTS},
    )

    assert result.accepted is True
    assert result.score >= 70
    assert result.document_signals is not None

    assert DocumentType.STATUTE.value in result.document_signals.document_types
    assert DocumentType.PRIIPS_KID.value in result.document_signals.document_types


def test_accepts_official_page_without_any_linked_document(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=AMBEAT_NAME,
        domain="ambeatgroup.cz",
        pages={"ambeatgroup.cz": OFFICIAL_PAGE_WITHOUT_DOCUMENTS},
    )

    assert result.accepted is True
    assert result.document_signals is not None
    assert result.document_signals.document_types == ()
    assert result.fund_context is True


def test_body_text_only_match_is_rejected_for_a_search_result(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="codyainvest.cz",
        pages={"codyainvest.cz": ADMINISTRATOR_PAGE},
    )

    assert result.accepted is False
    assert result.name_signals is not None
    assert result.name_signals.in_title is False
    assert result.name_signals.in_text is True

    assert "only in the body text" in result.reason


def test_body_text_only_match_is_accepted_from_a_manager_adapter(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="codyainvest.cz",
        pages={"codyainvest.cz": ADMINISTRATOR_PAGE},
        source=CandidateSourceName.ADAPTER,
    )

    assert result.accepted is True
    assert result.name_signals is not None
    assert result.name_signals.in_text is True


def test_rejects_unrelated_company_sharing_a_name_token(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="adax.cz",
        pages={"adax.cz": UNRELATED_COMPANY_PAGE},
    )

    assert result.accepted is False
    assert "does not clearly identify the fund" in result.reason


def test_rejects_parked_domain(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="adax.eu",
        pages={"adax.eu": PARKED_PAGE},
    )

    assert result.accepted is False
    assert result.parked is True
    assert "parked" in result.reason


def test_verifies_the_exact_result_page_before_the_homepage(
    tmp_path: Path,
) -> None:
    requested_paths: list[str] = []

    manager_homepage = b"""
    <html>
      <head><title>CODYA invest</title></head>
      <body><h1>Sprava fondu kvalifikovanych investoru</h1></body>
    </html>
    """

    fund_profile = b"""
    <html>
      <head><title>ADAX Fond firemniho nastupnictvi SICAV, a.s.</title></head>
      <body>
        <h1>ADAX Fond firemniho nastupnictvi SICAV, a.s.</h1>
        <p>Fond kvalifikovanych investoru.</p>
      </body>
    </html>
    """

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        requested_paths.append(request.url.path)

        content = fund_profile if request.url.path.startswith("/fondy/") else manager_homepage

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=content,
            request=request,
        )

    async def run_test() -> VerificationResult:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        candidate = DomainCandidate(
            domain="codyainvest.cz",
            url="https://codyainvest.cz/",
            source=CandidateSourceName.ADAPTER,
            rank=0,
            evidence="codya fund profile",
            result_url="https://codyainvest.cz/fondy/adax",
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            return await verify_candidate(
                fund_name=FUND_NAME,
                candidate=candidate,
                fetcher=fetcher,
            )

    result = asyncio.run(run_test())

    assert result.accepted is True

    # The stored URL is the fund profile, not the manager homepage.
    assert result.page_url == "https://codyainvest.cz/fondy/adax"

    assert requested_paths[0] == "/fondy/adax"


def test_penalizes_a_page_matching_only_one_word_of_the_name(
    tmp_path: Path,
) -> None:
    partial_page = b"""
    <html>
      <head><title>ADAX topidla</title></head>
      <body>
        <h1>ADAX</h1>
        <p>Investicni fond do topidel? Ne, vyrabime topidla.</p>
      </body>
    </html>
    """

    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="adax.cz",
        pages={"adax.cz": partial_page},
    )

    assert result.accepted is False
    assert result.name_signals is not None
    assert result.name_signals.distinctive_coverage < 1.0

    # The partial match is pushed below the review threshold.
    assert result.score < 40


def test_reports_unreachable_candidate_as_error(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="missing.cz",
        pages={},
    )

    assert result.accepted is False
    assert result.error is not None
    assert result.page_url is None


def test_name_signals_require_every_distinctive_token() -> None:
    content = extract_page_content(
        body=OFFICIAL_PAGE_WITH_DOCUMENTS,
        title="ADAX Fond firemniho nastupnictvi SICAV, a.s.",
    )

    signals = evaluate_name_signals(
        fund_name=FUND_NAME,
        content=content,
        domain="adaxfond.cz",
    )

    assert signals.distinctive_coverage == 1.0
    assert signals.in_title is True
    assert signals.hostname_token_match is True
    assert signals.identified is True

    unrelated_content = extract_page_content(
        body=UNRELATED_COMPANY_PAGE,
        title="Adax - vyroba elektrickych topidel",
    )

    unrelated_signals = evaluate_name_signals(
        fund_name=FUND_NAME,
        content=unrelated_content,
        domain="adax.cz",
    )

    assert unrelated_signals.distinctive_coverage < 1.0
    assert unrelated_signals.identified is False


def test_generic_tokens_alone_do_not_identify_a_fund() -> None:
    content = extract_page_content(
        body=OFFICIAL_PAGE_WITHOUT_DOCUMENTS,
        title="AMBEAT INVEST",
    )

    signals = evaluate_name_signals(
        fund_name="INVEST SICAV podfond, a.s.",
        content=content,
        domain="ambeatgroup.cz",
    )

    assert signals.distinctive_tokens == ()
    assert signals.identified is False


def test_detects_parked_pages_from_markers() -> None:
    content = extract_page_content(
        body=PARKED_PAGE,
        title="adax.eu",
    )

    assert is_parked_page(
        page_url="https://adax.eu/",
        content=content,
    )

    assert is_parked_page(
        page_url="https://trusted.domainseller.site/adax.eu",
        content=PageContent(
            title="ADAX",
            headings="",
            site_name="",
            visible_text="",
        ),
    )


VELARIS_NAME = "Velaris Fond SICAV a.s."


# A design studio that shares exactly one word with the fund name.
UNRELATED_ONE_WORD_PAGE = b"""
<html>
  <head><title>Velaris | Design studio</title></head>
  <body>
    <h1>Velaris</h1>
    <p>Navrhujeme interiery a nabytek. Investicni fond do designu neni nas obor.</p>
  </body>
</html>
"""


VELARIS_FUND_PAGE = b"""
<html>
  <head><title>Velaris Fund</title></head>
  <body>
    <h1>Velaris Fund SICAV</h1>
    <p>Fond kvalifikovanych investoru.</p>
  </body>
</html>
"""


def test_single_shared_word_is_not_enough_for_verification(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=VELARIS_NAME,
        domain="velaris.com",
        pages={"velaris.com": UNRELATED_ONE_WORD_PAGE},
    )

    assert result.accepted is False
    assert result.name_signals is not None
    assert result.name_signals.strong_locations == ()

    assert "multi-word fund name match" in result.reason

    assert any("no multi-word fund name match" in line for line in result.evidence)


def test_strong_multi_word_match_verifies_a_fund_site(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=VELARIS_NAME,
        domain="velarisfund.com",
        pages={"velarisfund.com": VELARIS_FUND_PAGE},
    )

    assert result.accepted is True
    assert result.name_signals is not None
    assert result.name_signals.matched_phrase == "velaris fund"

    assert any("name match" in line for line in result.evidence)
    assert result.evidence[-1].startswith("=")


def test_url_path_and_manager_information_count_as_evidence() -> None:
    profile_page = b"""
    <html>
      <head>
        <title>Detail fondu</title>
        <meta name="description" content="AMBEAT INVEST SICAV spravuje CODYA invest." />
      </head>
      <body>
        <h1>Detail fondu</h1>
        <footer>Obhospodarovatel: CODYA investicni spolecnost</footer>
      </body>
    </html>
    """

    content = extract_page_content(
        body=profile_page,
        title="Detail fondu",
        page_url="https://www.codyainvest.cz/nase-fondy/ambeat-invest-sicav",
    )

    signals = evaluate_name_signals(
        fund_name="AMBEAT INVEST SICAV, a.s.",
        content=content,
        domain="codyainvest.cz",
    )

    assert signals.identified is True

    assert MatchLocation.URL_PATH in signals.strong_locations
    assert MatchLocation.MANAGER_INFO in signals.strong_locations


def test_name_phrases_never_allow_a_single_word() -> None:
    phrases = name_phrases(VELARIS_NAME)

    assert phrases
    assert all(len(phrase.split()) >= 2 for phrase in phrases)

    # The English wording used on the fund website is covered.
    assert "velaris fund" in phrases

    # A name built only from generic wording yields the complete name.
    generic_phrases = name_phrases("Investiční fond SICAV, a.s.")

    assert all(len(phrase.split()) >= 2 for phrase in generic_phrases)


def test_evidence_explains_a_rejected_candidate(
    tmp_path: Path,
) -> None:
    result = verify(
        tmp_path=tmp_path,
        fund_name=FUND_NAME,
        domain="adax.cz",
        pages={"adax.cz": UNRELATED_COMPANY_PAGE},
    )

    assert result.accepted is False
    assert result.evidence

    joined = " | ".join(result.evidence)

    assert "no multi-word fund name match" in joined
    assert "rejected" in result.evidence[-1]


def test_news_and_announcement_pages_are_not_used_as_evidence(
    tmp_path: Path,
) -> None:
    requested_paths: list[str] = []

    announcement = b"""
    <html>
      <head><title>Oznameni o zmenach ve fondu</title></head>
      <body>
        <h1>Oznameni o zmenach ve fondu Velaris Fond SICAV, a.s.</h1>
        <p>Fond kvalifikovanych investoru.</p>
      </body>
    </html>
    """

    manager_homepage = b"""
    <html>
      <head><title>Spravce fondu</title></head>
      <body><h1>Sprava fondu kvalifikovanych investoru</h1></body>
    </html>
    """

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        requested_paths.append(request.url.path)

        content = announcement if request.url.path.startswith("/oznameni/") else manager_homepage

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=content,
            request=request,
        )

    async def run_test() -> VerificationResult:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        candidate = DomainCandidate(
            domain="manager.cz",
            url="https://manager.cz/",
            source=CandidateSourceName.SEARCH,
            rank=0,
            evidence="search result",
            result_url="https://manager.cz/oznameni/zmena-ve-fondu-velaris",
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            return await verify_candidate(
                fund_name=VELARIS_NAME,
                candidate=candidate,
                fetcher=fetcher,
            )

    result = asyncio.run(run_test())

    assert result.accepted is False

    # The announcement was never downloaded as evidence.
    assert all(not path.startswith("/oznameni/") for path in requested_paths)


def test_rejected_homepage_does_not_block_a_later_fund_profile(
    tmp_path: Path,
) -> None:
    manager_homepage = b"""
    <html>
      <head><title>CODYA invest</title></head>
      <body><h1>Sprava fondu kvalifikovanych investoru</h1></body>
    </html>
    """

    fund_profile = b"""
    <html>
      <head><title>ADAX Fond firemniho nastupnictvi SICAV, a.s.</title></head>
      <body>
        <h1>ADAX Fond firemniho nastupnictvi SICAV, a.s.</h1>
        <p>Fond kvalifikovanych investoru.</p>
      </body>
    </html>
    """

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        content = fund_profile if request.url.path.startswith("/fondy/") else manager_homepage

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=content,
            request=request,
        )

    async def run_test() -> tuple[VerificationResult, VerificationResult]:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        cache = DomainVerificationCache()

        homepage_candidate = DomainCandidate(
            domain="codyainvest.cz",
            url="https://codyainvest.cz/",
            source=CandidateSourceName.SEARCH,
            rank=0,
            evidence="generic search result",
        )

        profile_candidate = DomainCandidate(
            domain="codyainvest.cz",
            url="https://codyainvest.cz/",
            source=CandidateSourceName.SEARCH,
            rank=1,
            evidence="fund profile search result",
            result_url="https://codyainvest.cz/fondy/adax",
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            homepage_result = await cache.verify(
                fund_name=FUND_NAME,
                candidate=homepage_candidate,
                fetcher=fetcher,
            )

            profile_result = await cache.verify(
                fund_name=FUND_NAME,
                candidate=profile_candidate,
                fetcher=fetcher,
            )

        return (
            homepage_result,
            profile_result,
        )

    homepage_result, profile_result = asyncio.run(run_test())

    assert homepage_result.accepted is False

    # The rejected homepage must not be reused for the fund profile.
    assert profile_result.accepted is True
    assert profile_result.page_url == "https://codyainvest.cz/fondy/adax"


def _large_amista_catalog(
    *,
    target_id: str,
    target_title: str,
) -> bytes:
    """Build a catalog whose target fund sits past the text limit."""

    filler_sections = "".join(
        f"""
        <div class="m-toggle" id="filler_{index}_sicav_a_s_">
          <label class="m-toggle__label">
            <span class="m-toggle__title">FILLER {index} SICAV a.s.</span>
          </label>
          <div class="m-toggle__content">
            <p>{"Informacni povinnosti dle paragrafu 241 ZISIF. " * 40}</p>
            <table><tbody><tr>
              <td class="first">31.12.2025</td>
              <td><a href="download.php?id={index}">Vyrocni zprava 2025</a> (pdf)</td>
            </tr></tbody></table>
          </div>
        </div>
        """
        for index in range(120)
    )

    return f"""
    <html>
      <head><title>Povinne informace | AMISTA</title></head>
      <body>
        <h1>Povinne informace</h1>
        {filler_sections}
        <div class="m-toggle" id="{target_id}">
          <label class="m-toggle__label">
            <span class="m-toggle__title">{target_title}</span>
          </label>
          <div class="m-toggle__content">
            <table><tbody><tr>
              <td class="first">31.12.2025</td>
              <td><a href="download.php?id=999">Statut fondu</a> (pdf)</td>
            </tr></tbody></table>
          </div>
        </div>
      </body>
    </html>
    """.encode()


def test_verifies_a_fund_section_of_an_oversized_catalog_page(
    tmp_path: Path,
) -> None:
    """
    JF Fund regression.

    Its AMISTA section sits far past the truncation applied to the whole
    catalog page, so whole-page verification never sees the fund name.
    """

    catalog = _large_amista_catalog(
        target_id="jf_fund_sicav_a_s_",
        target_title="JF Fund SICAV a.s.",
    )

    assert len(catalog) > MAXIMUM_VERIFIED_TEXT_CHARACTERS

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=catalog,
            request=request,
        )

    async def run_test() -> tuple[VerificationResult, VerificationResult]:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        section_candidate = DomainCandidate(
            domain="amista.cz",
            url="https://amista.cz/",
            source=CandidateSourceName.ADAPTER,
            rank=0,
            evidence="amista fund profile",
            result_url=("https://amista.cz/povinne-informace.html#jf_fund_sicav_a_s_"),
        )

        page_candidate = DomainCandidate(
            domain="amista.cz",
            url="https://amista.cz/",
            source=CandidateSourceName.ADAPTER,
            rank=1,
            evidence="amista catalog page",
            result_url="https://amista.cz/povinne-informace.html",
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            section_result = await verify_candidate(
                fund_name="JF Fund SICAV a.s.",
                candidate=section_candidate,
                fetcher=fetcher,
            )

            page_result = await verify_candidate(
                fund_name="JF Fund SICAV a.s.",
                candidate=page_candidate,
                fetcher=fetcher,
            )

        return (
            section_result,
            page_result,
        )

    section_result, page_result = asyncio.run(run_test())

    assert section_result.accepted is True
    assert section_result.name_signals is not None

    # The fund is confirmed against its own section, not the whole page.
    assert MatchLocation.HEADING in section_result.name_signals.strong_locations
    assert MatchLocation.URL_PATH in section_result.name_signals.strong_locations

    # Without the section anchor the same page proves nothing.
    assert page_result.accepted is False


def test_section_scope_falls_back_to_the_whole_page(
    tmp_path: Path,
) -> None:
    page = b"""
    <html>
      <head><title>ADAX Fond firemniho nastupnictvi SICAV, a.s.</title></head>
      <body>
        <h1>ADAX Fond firemniho nastupnictvi SICAV, a.s.</h1>
        <p>Fond kvalifikovanych investoru.</p>
        <div id="jina_sekce"><a href="/statut.pdf">Statut fondu</a></div>
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
            content=page,
            request=request,
        )

    async def run_test() -> VerificationResult:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        candidate = DomainCandidate(
            domain="adaxfond.cz",
            url="https://adaxfond.cz/",
            source=CandidateSourceName.SEARCH,
            rank=0,
            evidence="search result",
            result_url="https://adaxfond.cz/#jina_sekce",
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            return await verify_candidate(
                fund_name=FUND_NAME,
                candidate=candidate,
                fetcher=fetcher,
            )

    result = asyncio.run(run_test())

    # The addressed section does not name the fund, but the page does.
    assert result.accepted is True


def test_document_signals_remain_a_confidence_bonus() -> None:
    signals = evaluate_document_signals(
        parsed_page_candidates=[],
        visible_text="Vyrocni zprava fondu je k dispozici na vyzadani.",
    )

    assert signals.document_types == ()
    assert signals.text_references
    assert signals.present is True

    generic_signals = evaluate_document_signals(
        parsed_page_candidates=[],
        visible_text="Statutarni organ spolecnosti zasedal v cervnu.",
    )

    assert generic_signals.present is False
