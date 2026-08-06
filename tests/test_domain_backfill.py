from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.domain_backfill import (
    DomainResolutionStatus,
    FundDomainResolution,
    build_missing_domain_report,
    load_fund_entries,
    resolve_missing_domains,
    select_funds_with_missing_domain,
    update_fund_domains,
    write_missing_domain_report,
)
from fundscraper.domain_candidates import (
    CandidateDiscovery,
    CandidateSourceName,
    DomainCandidate,
    HeuristicDomainGuesser,
)
from fundscraper.http_client import HttpFetcher

ADAX_NAME = "ADAX Fond firemního nástupnictví SICAV, a.s."
BRIXX_NAME = "BRIXX SICAV, a.s."
EXISTING_NAME = "3M FUND MSI SICAV a.s."


ADAX_PAGE = b"""
<html>
  <head>
    <title>ADAX Fond firemniho nastupnictvi SICAV, a.s.</title>
  </head>
  <body>
    <h1>ADAX Fond firemniho nastupnictvi SICAV, a.s.</h1>

    <a href="/dokumenty/statut.pdf">Statut fondu</a>

    <a href="/dokumenty/kid.pdf">Sdeleni klicovych informaci</a>
  </body>
</html>
"""


UNRELATED_PAGE = b"""
<html>
  <head>
    <title>Stavebniny Brixx s.r.o.</title>
  </head>
  <body>
    <h1>Prodej stavebnin</h1>

    <a href="/kontakt">Kontakt</a>
  </body>
</html>
"""


class StaticCandidateSource:
    """Return pre-defined candidates so tests never call a search engine."""

    name = "static"

    def __init__(
        self,
        candidates_by_fund: dict[str, tuple[str, ...]],
    ) -> None:
        self.candidates_by_fund = candidates_by_fund

    async def find(
        self,
        *,
        fund_name: str,
        fetcher: HttpFetcher,
        force: bool = False,
    ) -> CandidateDiscovery:
        domains = self.candidates_by_fund.get(fund_name, ())

        return CandidateDiscovery(
            candidates=tuple(
                DomainCandidate(
                    domain=domain,
                    url=f"https://{domain}/",
                    source=CandidateSourceName.SEARCH,
                    rank=rank,
                    evidence="static",
                )
                for rank, domain in enumerate(domains)
            ),
            warnings=(),
        )


def write_input_file(
    path: Path,
) -> None:
    payload = [
        {
            "name": EXISTING_NAME,
            "web": "https://www.3mfund.cz/",
        },
        {
            "name": ADAX_NAME,
            "web": None,
        },
        {
            "name": BRIXX_NAME,
            "web": None,
        },
    ]

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_handler(
    pages: dict[str, bytes],
) -> httpx.MockTransport:
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

    return httpx.MockTransport(handler)


def run_resolution(
    *,
    tmp_path: Path,
    input_path: Path,
    pages: dict[str, bytes],
    candidates_by_fund: dict[str, tuple[str, ...]],
) -> list[FundDomainResolution]:
    entries = select_funds_with_missing_domain(load_fund_entries(input_path))

    async def run_test() -> list[FundDomainResolution]:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=build_handler(pages),
        ) as fetcher:
            return await resolve_missing_domains(
                entries=entries,
                fetcher=fetcher,
                sources=(StaticCandidateSource(candidates_by_fund),),
                fallback_sources=(),
                concurrency=2,
            )

    return asyncio.run(run_test())


def test_selects_only_funds_with_missing_domain(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    entries = load_fund_entries(input_path)

    missing = select_funds_with_missing_domain(entries)

    assert len(entries) == 3

    assert [entry.name for entry in missing] == [
        ADAX_NAME,
        BRIXX_NAME,
    ]

    assert [entry.index for entry in missing] == [1, 2]


def test_accepts_verified_domain_and_updates_input_file(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={"adax.cz": ADAX_PAGE},
        candidates_by_fund={ADAX_NAME: ("adax.cz",)},
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    assert by_name[ADAX_NAME].status is DomainResolutionStatus.VERIFIED
    assert by_name[ADAX_NAME].resolved_url == "https://adax.cz/"

    updated = update_fund_domains(
        path=input_path,
        resolutions=resolutions,
    )

    assert updated == 1

    payload = json.loads(input_path.read_text(encoding="utf-8"))

    assert payload[1]["web"] == "https://adax.cz/"


def test_uncertain_evidence_becomes_review_required(
    tmp_path: Path,
) -> None:
    # The distinctive words of the fund name appear scattered in body
    # text, never as the legal name, on an unrelated hostname. That is
    # enough to be worth a look but not enough to store automatically.
    weak_page = b"""
    <html>
      <head><title>Prehled trhu</title></head>
      <body>
        <h1>Prehled trhu</h1>
        <p>
          Fond ADAX se zameruje na nastupnictvi ve firemniho sektoru.
        </p>
      </body>
    </html>
    """

    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={"prehledtrhu.cz": weak_page},
        candidates_by_fund={ADAX_NAME: ("prehledtrhu.cz",)},
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    adax_resolution = by_name[ADAX_NAME]

    assert adax_resolution.status is DomainResolutionStatus.REVIEW_REQUIRED
    assert adax_resolution.resolved_url is None

    assert adax_resolution.candidates[0].evidence

    updated = update_fund_domains(
        path=input_path,
        resolutions=resolutions,
    )

    assert updated == 0


def test_rejects_unrelated_page(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={"brixx.cz": UNRELATED_PAGE},
        candidates_by_fund={BRIXX_NAME: ("brixx.cz",)},
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    assert by_name[BRIXX_NAME].status is not DomainResolutionStatus.VERIFIED
    assert by_name[BRIXX_NAME].resolved_url is None

    updated = update_fund_domains(
        path=input_path,
        resolutions=resolutions,
    )

    assert updated == 0

    payload = json.loads(input_path.read_text(encoding="utf-8"))

    assert payload[2]["web"] is None


def test_treats_the_same_brand_under_several_domains_as_one_website(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={
            "adax.cz": ADAX_PAGE,
            "adax.eu": ADAX_PAGE,
        },
        candidates_by_fund={
            ADAX_NAME: (
                "adax.eu",
                "adax.cz",
            )
        },
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    adax_resolution = by_name[ADAX_NAME]

    assert adax_resolution.status is DomainResolutionStatus.VERIFIED
    assert adax_resolution.resolved_url == "https://adax.cz/"


def test_third_party_mention_does_not_compete_with_the_fund_domain(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={
            "adax.cz": ADAX_PAGE,
            "portal.eu": ADAX_PAGE,
        },
        candidates_by_fund={
            ADAX_NAME: (
                "portal.eu",
                "adax.cz",
            )
        },
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    adax_resolution = by_name[ADAX_NAME]

    assert adax_resolution.status is DomainResolutionStatus.VERIFIED
    assert adax_resolution.resolved_url == "https://adax.cz/"


def test_reports_ambiguous_candidates_as_review_required(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={
            "adaxfond.cz": ADAX_PAGE,
            "adaxinvest.cz": ADAX_PAGE,
        },
        candidates_by_fund={
            ADAX_NAME: (
                "adaxfond.cz",
                "adaxinvest.cz",
            )
        },
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    adax_resolution = by_name[ADAX_NAME]

    assert adax_resolution.status is DomainResolutionStatus.REVIEW_REQUIRED
    assert adax_resolution.resolved_url is None
    assert "ambiguous" in adax_resolution.reason

    assert len(adax_resolution.candidates) == 2


def test_prefers_a_fund_hostname_over_a_bare_name_hostname(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={
            "adax.cz": ADAX_PAGE,
            "adaxfond.cz": ADAX_PAGE,
        },
        candidates_by_fund={
            ADAX_NAME: (
                "adax.cz",
                "adaxfond.cz",
            )
        },
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    adax_resolution = by_name[ADAX_NAME]

    assert adax_resolution.status is DomainResolutionStatus.VERIFIED
    assert adax_resolution.resolved_url == "https://adaxfond.cz/"


def test_writes_missing_urls_report(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={
            "adax.cz": ADAX_PAGE,
            "brixx.cz": UNRELATED_PAGE,
        },
        candidates_by_fund={
            ADAX_NAME: ("adax.cz",),
            BRIXX_NAME: ("brixx.cz",),
        },
    )

    domains_added = update_fund_domains(
        path=input_path,
        resolutions=resolutions,
    )

    report = build_missing_domain_report(
        input_path=input_path,
        funds_total=3,
        resolutions=resolutions,
        domains_added=domains_added,
    )

    report_path = tmp_path / "missing_urls.json"

    write_missing_domain_report(
        report=report,
        path=report_path,
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["summary"]["funds_checked"] == 2
    assert payload["summary"]["domains_added"] == 1
    assert payload["summary"]["unresolved"] == 1

    assert len(payload["funds"]) == 1

    unresolved = payload["funds"][0]

    assert unresolved["name"] == BRIXX_NAME
    assert unresolved["fund_id"].startswith("fund_")
    assert unresolved["status"] in {
        "not_found",
        "review_required",
    }
    assert unresolved["reason"]
    assert unresolved["candidates"][0]["domain"] == "brixx.cz"


def test_preserves_existing_values_and_file_formatting(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    original_text = input_path.read_text(encoding="utf-8")

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={"adax.cz": ADAX_PAGE},
        candidates_by_fund={ADAX_NAME: ("adax.cz",)},
    )

    update_fund_domains(
        path=input_path,
        resolutions=resolutions,
    )

    updated_text = input_path.read_text(encoding="utf-8")

    payload = json.loads(updated_text)

    assert [item["name"] for item in payload] == [
        EXISTING_NAME,
        ADAX_NAME,
        BRIXX_NAME,
    ]

    assert payload[0]["web"] == "https://www.3mfund.cz/"
    assert payload[2]["web"] is None

    assert list(payload[1].keys()) == ["name", "web"]

    assert updated_text.endswith("}\n]\n")
    assert '\n  {\n    "name"' in updated_text

    assert updated_text == original_text.replace(
        '"name": "ADAX Fond firemního nástupnictví SICAV, a.s.",\n    "web": null',
        '"name": "ADAX Fond firemního nástupnictví SICAV, a.s.",\n    "web": "https://adax.cz/"',
    )


def test_dry_run_does_not_modify_input_file(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    original_text = input_path.read_text(encoding="utf-8")

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={"adax.cz": ADAX_PAGE},
        candidates_by_fund={ADAX_NAME: ("adax.cz",)},
    )

    updated = update_fund_domains(
        path=input_path,
        resolutions=resolutions,
        dry_run=True,
    )

    assert updated == 1

    assert input_path.read_text(encoding="utf-8") == original_text


def test_failed_guessed_domains_are_not_reported_as_errors(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    entries = select_funds_with_missing_domain(load_fund_entries(input_path))

    async def run_test() -> list[FundDomainResolution]:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=build_handler({}),
        ) as fetcher:
            return await resolve_missing_domains(
                entries=entries,
                fetcher=fetcher,
                sources=(),
                fallback_sources=(HeuristicDomainGuesser(max_results=3),),
                adapter_sources=(),
                concurrency=2,
            )

    resolutions = asyncio.run(run_test())

    assert resolutions

    for resolution in resolutions:
        # Every candidate was a generated guess that did not resolve.
        assert resolution.candidates
        assert all(result.error is not None for result in resolution.candidates)

        assert resolution.status is DomainResolutionStatus.NOT_FOUND


def test_verified_section_url_keeps_its_fund_anchor(
    tmp_path: Path,
) -> None:
    catalog_page = b"""
    <html>
      <head><title>Povinne informace</title></head>
      <body>
        <h1>Povinne informace</h1>
        <div id="adax_fond_firemniho_nastupnictvi">
          <span>ADAX Fond firemniho nastupnictvi SICAV, a.s.</span>
          <p>Fond kvalifikovanych investoru.</p>
          <a href="/download.php?id=1">Statut fondu</a>
        </div>
      </body>
    </html>
    """

    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    entries = select_funds_with_missing_domain(load_fund_entries(input_path))

    class SectionSource:
        name = CandidateSourceName.ADAPTER

        async def find(
            self,
            *,
            fund_name: str,
            fetcher: HttpFetcher,
            force: bool = False,
        ) -> CandidateDiscovery:
            if fund_name != ADAX_NAME:
                return CandidateDiscovery(
                    candidates=(),
                    warnings=(),
                )

            return CandidateDiscovery(
                candidates=(
                    DomainCandidate(
                        domain="amista.cz",
                        url="https://amista.cz/",
                        source=CandidateSourceName.ADAPTER,
                        rank=0,
                        evidence="amista fund profile",
                        result_url=(
                            "https://amista.cz/povinne-informace.html"
                            "#adax_fond_firemniho_nastupnictvi"
                        ),
                    ),
                ),
                warnings=(),
            )

    async def run_test() -> list[FundDomainResolution]:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=build_handler({"amista.cz": catalog_page}),
        ) as fetcher:
            return await resolve_missing_domains(
                entries=entries,
                fetcher=fetcher,
                sources=(),
                fallback_sources=(),
                adapter_sources=(SectionSource(),),
                concurrency=1,
            )

    resolutions = asyncio.run(run_test())

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    adax_resolution = by_name[ADAX_NAME]

    assert adax_resolution.status is DomainResolutionStatus.VERIFIED

    assert adax_resolution.resolved_url == (
        "https://amista.cz/povinne-informace.html#adax_fond_firemniho_nastupnictvi"
    )


def test_failed_fund_does_not_stop_remaining_funds(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input_file(input_path)

    resolutions = run_resolution(
        tmp_path=tmp_path,
        input_path=input_path,
        pages={"adax.cz": ADAX_PAGE},
        candidates_by_fund={
            ADAX_NAME: ("adax.cz",),
            BRIXX_NAME: ("unreachable.cz",),
        },
    )

    by_name = {resolution.entry.name: resolution for resolution in resolutions}

    assert len(resolutions) == 2

    assert by_name[ADAX_NAME].status is DomainResolutionStatus.VERIFIED
    assert by_name[BRIXX_NAME].status is DomainResolutionStatus.ERROR
