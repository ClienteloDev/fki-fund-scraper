from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.database import (
    initialize_database,
    list_discovery_entries,
    register_funds,
)
from fundscraper.discovery_priority import DiscoveryMethod
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.official_discovery import (
    REJECTED_OFF_DOMAIN,
    REJECTED_OTHER_FUND,
    REJECTED_ROBOTS,
    OfficialDiscoveryResult,
    discover_official_sources,
)
from fundscraper.output_models import DocumentType
from fundscraper.output_service import stable_fund_id

STANDALONE_FUND = FundInput(
    name="Rezidento Alfa SICAV, a.s.",
    web="https://www.rezidentoalfa.cz",
)


MANAGER_HOSTED_FUND = FundInput(
    name="Rezidento Alfa SICAV, a.s.",
    web="https://www.spravce.cz/nase-fondy/rezidento-alfa-sicav",
)


def _html(body: str) -> bytes:
    return f"<html><head><title>Rezidento Alfa</title></head><body>{body}</body></html>".encode()


def _run(
    *,
    fund: FundInput,
    pages: dict[str, tuple[bytes, str]],
    tmp_path: Path,
    database_path: Path | None = None,
    max_pages: int = 15,
) -> OfficialDiscoveryResult:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"

        entry = pages.get(key)

        if entry is None:
            return httpx.Response(
                status_code=404,
                request=request,
            )

        content, content_type = entry

        return httpx.Response(
            status_code=200,
            headers={"Content-Type": content_type},
            content=content,
            request=request,
        )

    async def run() -> OfficialDiscoveryResult:
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
            return await discover_official_sources(
                database_path=database_path,
                fund=fund,
                fetcher=fetcher,
                max_pages=max_pages,
            )

    return asyncio.run(run())


STANDALONE_PAGES: dict[str, tuple[bytes, str]] = {
    "www.rezidentoalfa.cz/robots.txt": (
        b"User-agent: *\nDisallow: /interni/\nSitemap: https://www.rezidentoalfa.cz/sitemap.xml\n",
        "text/plain",
    ),
    "www.rezidentoalfa.cz/sitemap.xml": (
        b"""<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.rezidentoalfa.cz/archiv-zprav/</loc></url>
          <url><loc>https://www.rezidentoalfa.cz/kariera/</loc></url>
          <url><loc>https://www.rezidentoalfa.cz/interni/statut-fondu-2026.pdf</loc></url>
        </urlset>""",
        "application/xml",
    ),
    "www.rezidentoalfa.cz/": (
        _html(
            """
            <a href="/pro-investory/">Pro investory</a>
            <a href="/kariera/">Kariéra</a>
            <a href="https://www.jinyweb.cz/dokumenty/statut.pdf">Statut jinde</a>
            """
        ),
        "text/html",
    ),
    "www.rezidentoalfa.cz/pro-investory/": (
        _html(
            """
            <a href="/dokumenty/sdeleni-klicovych-informaci-2026.pdf">Stáhnout</a>
            <a href="/dokumenty/statut-fondu.pdf">Statut fondu</a>
            <a href="/dokumenty/vyrocni-zprava-2024.pdf">Výroční zpráva 2024</a>
            """
        ),
        "text/html",
    ),
    "www.rezidentoalfa.cz/archiv-zprav/": (
        _html('<a href="/dokumenty/vyrocni-zprava-2019.pdf">Výroční zpráva 2019</a>'),
        "text/html",
    ),
}


def test_finds_the_official_documents_of_a_standalone_site(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    found = {document.url for document in result.documents}

    assert "https://www.rezidentoalfa.cz/dokumenty/sdeleni-klicovych-informaci-2026.pdf" in found

    assert "https://www.rezidentoalfa.cz/dokumenty/statut-fondu.pdf" in found

    assert "https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2024.pdf" in found

    types = {document.document_type for document in result.documents}

    assert DocumentType.PRIIPS_KID in types

    assert DocumentType.STATUTE in types


def test_reaches_an_archive_only_the_sitemap_links(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    # Nothing in the navigation points at the archive. Only the sitemap
    # does, which is the reason sitemap support exists.
    assert "https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2019.pdf" in {
        document.url for document in result.documents
    }

    assert result.metrics.sitemaps_read == 1

    assert result.metrics.method_counts.get(DiscoveryMethod.SITEMAP.value)


def test_keeps_the_older_version_of_a_document_below_the_newer_one(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    by_url = {document.url: document for document in result.documents}

    recent = by_url["https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2024.pdf"]

    old = by_url["https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2019.pdf"]

    assert recent.score > old.score


def test_refuses_a_path_robots_closed(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    rejected = {entry.url: entry.rejection_reason for entry in result.entries if not entry.accepted}

    blocked = "https://www.rezidentoalfa.cz/interni/statut-fondu-2026.pdf"

    assert rejected.get(blocked) == REJECTED_ROBOTS


def test_never_leaves_the_official_domain(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    assert all("jinyweb.cz" not in document.url for document in result.documents)

    rejected = {entry.url: entry.rejection_reason for entry in result.entries if not entry.accepted}

    assert rejected.get("https://www.jinyweb.cz/dokumenty/statut.pdf") == REJECTED_OFF_DOMAIN


def test_the_stage_reports_itself_exhausted_when_it_found_the_key_documents(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    assert result.is_exhausted


MANAGER_PAGES: dict[str, tuple[bytes, str]] = {
    "www.spravce.cz/robots.txt": (
        b"User-agent: *\n",
        "text/plain",
    ),
    "www.spravce.cz/nase-fondy/rezidento-alfa-sicav": (
        (
            "<html><head><title>Rezidento Alfa SICAV, a.s.</title></head><body>"
            '<a href="/dokumenty/rezidento-alfa-statut.pdf">Statut fondu</a>'
            '<a href="/nase-fondy/jiny-fond-sicav">Jiný fond SICAV</a>'
            "</body></html>"
        ).encode(),
        "text/html",
    ),
    "www.spravce.cz/nase-fondy/jiny-fond-sicav": (
        (
            "<html><head><title>Jiný fond SICAV, a.s.</title></head><body>"
            '<a href="/dokumenty/jiny-fond-statut.pdf">Statut fondu</a>'
            "</body></html>"
        ).encode(),
        "text/html",
    ),
}


def test_attributes_a_manager_site_by_section_not_by_domain(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=MANAGER_HOSTED_FUND,
        pages=MANAGER_PAGES,
        tmp_path=tmp_path,
    )

    accepted = {document.url for document in result.documents}

    assert "https://www.spravce.cz/dokumenty/rezidento-alfa-statut.pdf" in accepted

    # The manager hosts the statute of every fund it runs. Being on the
    # same domain is not enough to claim one.
    assert "https://www.spravce.cz/dokumenty/jiny-fond-statut.pdf" not in accepted

    rejected = {entry.url: entry.rejection_reason for entry in result.entries if not entry.accepted}

    assert rejected.get("https://www.spravce.cz/dokumenty/jiny-fond-statut.pdf") == (
        REJECTED_OTHER_FUND
    )


def test_records_every_decision_in_the_discovery_log(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "processing.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [STANDALONE_FUND],
    )

    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
        database_path=database_path,
    )

    stored = list_discovery_entries(
        database_path,
        fund_id=stable_fund_id(STANDALONE_FUND),
    )

    assert len(stored) == len(result.entries)

    by_url = {entry.url: entry for entry in stored}

    statute = by_url["https://www.rezidentoalfa.cz/dokumenty/statut-fondu.pdf"]

    assert statute.accepted

    assert statute.is_document

    assert statute.method == DiscoveryMethod.DOCUMENT_LINK.value

    assert statute.discovered_from == "https://www.rezidentoalfa.cz/pro-investory/"

    assert statute.document_type == DocumentType.STATUTE.value

    assert statute.scope_decision is not None

    assert statute.priority_score > 0

    blocked = by_url["https://www.rezidentoalfa.cz/interni/statut-fondu-2026.pdf"]

    assert not blocked.accepted

    assert blocked.rejection_reason == REJECTED_ROBOTS


def test_reports_metrics_for_one_fund(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    metrics = result.metrics

    assert metrics.pages_fetched > 0

    assert metrics.documents_accepted == len(result.documents)

    assert metrics.official_source_hits == len(result.documents)

    assert sum(metrics.method_counts.values()) == metrics.pages_fetched


REDIRECTING_PAGES: dict[str, tuple[bytes, str]] = {
    "www.rezidentoalfa.cz/": (
        _html('<a href="/povinne-informace">Povinné informace</a>'),
        "text/html",
    ),
    "www.rezidentoalfa.cz/povinne-informace": (
        _html('<a href="/dokumenty/statut-fondu.pdf">Statut fondu</a>'),
        "text/html",
    ),
}


def test_follows_links_of_the_address_that_answered(
    tmp_path: Path,
) -> None:
    """A bare host redirecting to its www form must not lose its paths."""

    def handler(request: httpx.Request) -> httpx.Response:
        # The bare host answers every path with the homepage, exactly as
        # a misconfigured redirect does.
        if request.url.host == "rezidentoalfa.cz":
            return httpx.Response(
                status_code=301,
                headers={"Location": "https://www.rezidentoalfa.cz/"},
                request=request,
            )

        entry = REDIRECTING_PAGES.get(f"{request.url.host}{request.url.path}")

        if entry is None:
            return httpx.Response(
                status_code=404,
                request=request,
            )

        return httpx.Response(
            status_code=200,
            headers={"Content-Type": entry[1]},
            content=entry[0],
            request=request,
        )

    async def run() -> OfficialDiscoveryResult:
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
            return await discover_official_sources(
                database_path=None,
                fund=FundInput(
                    name="Rezidento Alfa SICAV, a.s.",
                    web="https://rezidentoalfa.cz",
                ),
                fetcher=fetcher,
                max_pages=6,
            )

    result = asyncio.run(run())

    assert "https://www.rezidentoalfa.cz/dokumenty/statut-fondu.pdf" in {
        document.url for document in result.documents
    }


EXTENSIONLESS_PAGES: dict[str, tuple[bytes, str]] = {
    "www.spravce.cz/nase-fondy/rezidento-alfa-sicav": (
        (
            "<html><head><title>Rezidento Alfa SICAV, a.s.</title></head><body>"
            '<a href="/file/sdff-get?id=7371">Statut Rezidento Alfa SICAV, a.s.</a>'
            '<a href="/file/sdff-get?id=7701">Výroční zpráva Rezidento Alfa 2025</a>'
            '<a href="/file/sdff-get?id=3115">Smlouva o poskytování služeb</a>'
            '<a href="/nase-fondy/rezidento-alfa-sicav/archiv">Archiv dokumentů</a>'
            "</body></html>"
        ).encode(),
        "text/html",
    ),
    "www.spravce.cz/nase-fondy/rezidento-alfa-sicav/archiv": (
        _html("<p>Archiv</p>"),
        "text/html",
    ),
}


def test_accepts_a_document_that_carries_no_file_extension(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=MANAGER_HOSTED_FUND,
        pages=EXTENSIONLESS_PAGES,
        tmp_path=tmp_path,
    )

    by_url = {document.url: document for document in result.documents}

    statute = by_url.get("https://www.spravce.cz/file/sdff-get?id=7371")

    assert statute is not None

    assert statute.document_type is DocumentType.STATUTE

    report = by_url.get("https://www.spravce.cz/file/sdff-get?id=7701")

    assert report is not None

    assert report.document_type is DocumentType.ANNUAL_REPORT

    # A service contract is not one of the official fund documents, and
    # a link to the archive stays a page worth crawling.
    assert "https://www.spravce.cz/file/sdff-get?id=3115" not in by_url

    assert "https://www.spravce.cz/nase-fondy/rezidento-alfa-sicav/archiv" not in by_url


KID_ONLY_PAGES: dict[str, tuple[bytes, str]] = {
    "www.rezidentoalfa.cz/": (
        _html('<a href="/dokumenty/sdeleni-klicovych-informaci-2026.pdf">Klíčové informace</a>'),
        "text/html",
    ),
}


SUFFICIENT_PAGES: dict[str, tuple[bytes, str]] = {
    "www.rezidentoalfa.cz/": (
        _html(
            """
            <a href="/dokumenty/sdeleni-klicovych-informaci-2026.pdf">Klíčové informace</a>
            <a href="/dokumenty/statut-fondu.pdf">Statut fondu</a>
            <a href="/dokumenty/vyrocni-zprava-2024.pdf">Výroční zpráva 2024</a>
            """
        ),
        "text/html",
    ),
}


def test_a_lone_key_information_document_is_not_sufficient(
    tmp_path: Path,
) -> None:
    """One KID leaves the governing and reporting documents unanswered."""

    result = _run(
        fund=STANDALONE_FUND,
        pages=KID_ONLY_PAGES,
        tmp_path=tmp_path,
    )

    assert DocumentType.PRIIPS_KID in {document.document_type for document in result.documents}

    # The official site was explored first and to the end...
    assert result.is_exhausted

    # ...but what it publishes does not answer everything, so a fallback
    # adapter must still be allowed to run.
    assert not result.is_sufficient

    assert set(result.missing_document_groups) == {
        "governing_document",
        "reporting_document",
    }


def test_a_complete_set_of_official_documents_is_sufficient(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=STANDALONE_FUND,
        pages=SUFFICIENT_PAGES,
        tmp_path=tmp_path,
    )

    found = {document.document_type for document in result.documents}

    assert DocumentType.PRIIPS_KID in found

    assert DocumentType.STATUTE in found

    assert DocumentType.ANNUAL_REPORT in found

    assert result.is_sufficient

    assert result.missing_document_groups == ()


def test_exploration_may_complete_without_finding_anything(
    tmp_path: Path,
) -> None:
    """Completing the crawl and finding enough are separate facts."""

    result = _run(
        fund=STANDALONE_FUND,
        pages={
            "www.rezidentoalfa.cz/": (
                _html("<p>Vítejte</p>"),
                "text/html",
            )
        },
        tmp_path=tmp_path,
    )

    assert result.is_exhausted

    assert not result.is_sufficient

    assert len(result.missing_document_groups) == 3


MIXED_MANAGER_PAGES: dict[str, tuple[bytes, str]] = {
    "www.spravce.cz/robots.txt": (
        b"User-agent: *\n",
        "text/plain",
    ),
    "www.spravce.cz/nase-fondy/rezidento-alfa-sicav": (
        (
            "<html><head><title>Rezidento Alfa SICAV, a.s.</title></head><body>"
            '<a href="/dokumenty/rezidento-alfa-kid.pdf">Klíčové informace</a>'
            '<a href="/nase-fondy/jiny-fond-sicav">Jiný fond SICAV</a>'
            "</body></html>"
        ).encode(),
        "text/html",
    ),
    "www.spravce.cz/nase-fondy/jiny-fond-sicav": (
        (
            "<html><head><title>Jiný fond SICAV, a.s.</title></head><body>"
            '<a href="/dokumenty/jiny-fond-statut.pdf">Statut fondu</a>'
            '<a href="/dokumenty/jiny-fond-vyrocni-zprava-2024.pdf">Výroční zpráva 2024</a>'
            "</body></html>"
        ).encode(),
        "text/html",
    ),
}


def test_unrelated_manager_documents_stay_rejected_when_sources_fall_short(
    tmp_path: Path,
) -> None:
    """
    A shortfall must never be filled from a neighbouring fund.

    The stage finds only a key information document for this fund, so it
    reports itself insufficient and a fallback becomes allowed. The
    statute and the annual report of the fund next to it on the same
    manager domain stay refused, and the missing groups stay missing.
    """

    result = _run(
        fund=MANAGER_HOSTED_FUND,
        pages=MIXED_MANAGER_PAGES,
        tmp_path=tmp_path,
    )

    accepted = {document.url for document in result.documents}

    assert "https://www.spravce.cz/dokumenty/rezidento-alfa-kid.pdf" in accepted

    assert "https://www.spravce.cz/dokumenty/jiny-fond-statut.pdf" not in accepted

    assert "https://www.spravce.cz/dokumenty/jiny-fond-vyrocni-zprava-2024.pdf" not in accepted

    rejected = {entry.url: entry.rejection_reason for entry in result.entries if not entry.accepted}

    assert rejected.get("https://www.spravce.cz/dokumenty/jiny-fond-statut.pdf") == (
        REJECTED_OTHER_FUND
    )

    assert rejected.get("https://www.spravce.cz/dokumenty/jiny-fond-vyrocni-zprava-2024.pdf") == (
        REJECTED_OTHER_FUND
    )

    # The shortfall is reported rather than filled from the documents of
    # the neighbouring fund.
    assert not result.is_sufficient

    assert set(result.missing_document_groups) == {
        "governing_document",
        "reporting_document",
    }


CROSS_LINKED_PAGES: dict[str, tuple[bytes, str]] = {
    "www.spravce.cz/robots.txt": (
        b"User-agent: *\n",
        "text/plain",
    ),
    "www.spravce.cz/nase-fondy/rezidento-alfa-sicav": (
        (
            "<html><head><title>Rezidento Alfa SICAV, a.s.</title></head><body>"
            # The fund links its own statute under a generic anchor...
            '<a href="/dokumenty/rezidento-alfa-statut.pdf">Stáhnout</a>'
            # ...its own statute naming itself...
            '<a href="/dokumenty/statut.pdf">Statut Rezidento Alfa SICAV, a.s.</a>'
            # ...and the statute of a neighbouring fund of the manager.
            '<a href="/dokumenty/jiny-fond-statut.pdf">Statut Jiný fond SICAV</a>'
            "</body></html>"
        ).encode(),
        "text/html",
    ),
}


def test_rejects_a_document_whose_link_names_another_fund(
    tmp_path: Path,
) -> None:
    """A neighbouring fund on this fund's own page is still not this fund."""

    result = _run(
        fund=MANAGER_HOSTED_FUND,
        pages=CROSS_LINKED_PAGES,
        tmp_path=tmp_path,
    )

    accepted = {document.url for document in result.documents}

    assert "https://www.spravce.cz/dokumenty/jiny-fond-statut.pdf" not in accepted

    rejected = {entry.url: entry.rejection_reason for entry in result.entries if not entry.accepted}

    assert rejected.get("https://www.spravce.cz/dokumenty/jiny-fond-statut.pdf") == (
        REJECTED_OTHER_FUND
    )


def test_accepts_a_document_whose_link_names_this_fund(
    tmp_path: Path,
) -> None:
    result = _run(
        fund=MANAGER_HOSTED_FUND,
        pages=CROSS_LINKED_PAGES,
        tmp_path=tmp_path,
    )

    by_url = {document.url: document for document in result.documents}

    own = by_url.get("https://www.spravce.cz/dokumenty/statut.pdf")

    assert own is not None

    assert own.document_type is DocumentType.STATUTE


def test_a_generic_anchor_keeps_a_fund_specific_document(
    tmp_path: Path,
) -> None:
    """Only a link naming another fund is refused, not an unnamed one."""

    result = _run(
        fund=MANAGER_HOSTED_FUND,
        pages=CROSS_LINKED_PAGES,
        tmp_path=tmp_path,
    )

    accepted = {document.url for document in result.documents}

    # The anchor says only "Stáhnout"; the address carries the fund.
    assert "https://www.spravce.cz/dokumenty/rezidento-alfa-statut.pdf" in accepted


def test_a_document_named_only_by_its_kind_is_not_read_as_another_fund(
    tmp_path: Path,
) -> None:
    """ "Statut fondu" names no fund at all and must stay accepted."""

    result = _run(
        fund=STANDALONE_FUND,
        pages=STANDALONE_PAGES,
        tmp_path=tmp_path,
    )

    assert "https://www.rezidentoalfa.cz/dokumenty/statut-fondu.pdf" in {
        document.url for document in result.documents
    }


def test_a_subfund_document_is_not_read_as_another_fund() -> None:
    """A subfund carries the name of its fund inside a longer word."""

    from fundscraper.official_discovery import _link_names_another_fund

    care = FundInput(
        name="CARE SICAV, a.s.",
        web="https://www.spravce.cz/nase-fondy/care-sicav-a-s",
    )

    assert not _link_names_another_fund(
        fund=care,
        link_url="https://www.spravce.cz/file/sdff-get?id=7371",
        anchor_text="Statut CARE SICAV, a.s. fond+ podfond",
    )

    assert not _link_names_another_fund(
        fund=care,
        link_url="https://www.spravce.cz/dokumenty/statut-podfondu-esg-seniorcare.pdf",
        anchor_text="Statut podfondu ESG SeniorCare",
    )

    assert _link_names_another_fund(
        fund=care,
        link_url="https://www.spravce.cz/dokumenty/statut.pdf",
        anchor_text="Statut Rezidento Alfa SICAV, a.s.",
    )
