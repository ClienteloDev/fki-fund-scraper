"""
What a guessed address that does not exist means, and what it does not.

Discovery asks every site for `/robots.txt` and for three conventional
sitemap addresses. A full run produced 874 "failures", of which 474 were
those guesses being answered and exactly one was a `MemoryError`. The
cases here keep the two apart, and prove that a site without any sitemap
is still read through its navigation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.official_discovery import (
    OfficialDiscoveryResult,
    discover_official_sources,
)
from fundscraper.output_models import DocumentType
from fundscraper.run_diagnostics import (
    Diagnostic,
    DiagnosticLevel,
    classify_fetch_failure,
    escalate_repeated_server_errors,
    is_guessed_address,
    summarize,
)

FUND = FundInput(
    name="MAEG Investment SICAV, a.s.",
    web="https://www.maeg-investment.cz",
)


def _html(body: str) -> bytes:
    return (f"<html><head><title>MAEG Investment</title></head><body>{body}</body></html>").encode()


# A site with no sitemap of any kind, whose documents are reachable only
# by following the investor section of its navigation.
NAVIGATION_ONLY_PAGES: dict[str, tuple[bytes, str]] = {
    "www.maeg-investment.cz/": (
        _html(
            """
            <a href="/pro-investory/">Pro investory</a>
            <a href="/kontakt/">Kontakt</a>
            """
        ),
        "text/html",
    ),
    "www.maeg-investment.cz/pro-investory/": (
        _html(
            """
            <a href="/dokumenty/">Dokumenty</a>
            <a href="/dokumenty/sdeleni-klicovych-informaci.pdf">KID</a>
            """
        ),
        "text/html",
    ),
    "www.maeg-investment.cz/dokumenty/": (
        _html(
            """
            <a href="/dokumenty/statut-fondu.pdf">Statut</a>
            <a href="/dokumenty/vyrocni-zprava-2024.pdf">Výroční zpráva 2024</a>
            """
        ),
        "text/html",
    ),
}


def run_discovery(
    *,
    pages: dict[str, tuple[bytes, str]],
    tmp_path: Path,
    server_error_paths: frozenset[str] = frozenset(),
) -> OfficialDiscoveryResult:
    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"

        if request.url.path in server_error_paths:
            return httpx.Response(
                status_code=503,
                request=request,
            )

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

    async def go() -> OfficialDiscoveryResult:
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
                fund=FUND,
                fetcher=fetcher,
                max_pages=12,
            )

    return asyncio.run(go())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_the_guessed_addresses_are_recognised() -> None:
    for path in (
        "/robots.txt",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-index.xml",
    ):
        assert is_guessed_address(f"https://example.cz{path}")

    assert not is_guessed_address("https://example.cz/dokumenty/statut.pdf")

    assert not is_guessed_address("https://example.cz/pro-investory/")


def test_a_missing_guessed_sitemap_is_an_expected_miss() -> None:
    assert (
        classify_fetch_failure(
            url="https://example.cz/sitemap_index.xml",
            code="http_status_error",
            status_code=404,
        )
        is DiagnosticLevel.EXPECTED_MISS
    )


def test_a_missing_linked_document_is_not_an_expected_miss() -> None:
    """Nobody guessed this address: something linked to it and it is gone."""

    assert (
        classify_fetch_failure(
            url="https://example.cz/dokumenty/statut.pdf",
            code="http_status_error",
            status_code=404,
        )
        is DiagnosticLevel.WARNING
    )


def test_an_unexpected_error_stays_a_failure() -> None:
    """A MemoryError must not be filed next to a guessed sitemap."""

    assert (
        classify_fetch_failure(
            url="https://example.cz/dokumenty/statut.pdf",
            code="memory_error",
        )
        is DiagnosticLevel.FAILURE
    )


def test_repeated_server_errors_from_one_host_become_a_failure() -> None:
    single = [
        Diagnostic(
            level=DiagnosticLevel.WARNING,
            stage="discovery",
            code="http_status_error",
            message="Server returned HTTP status 503",
            url="https://slow.example.cz/a.pdf",
        )
    ]

    assert escalate_repeated_server_errors(single) == single

    repeated = [
        Diagnostic(
            level=DiagnosticLevel.WARNING,
            stage="discovery",
            code="http_status_error",
            message="Server returned HTTP status 503",
            url=f"https://slow.example.cz/{index}.pdf",
        )
        for index in range(3)
    ]

    escalated = escalate_repeated_server_errors(repeated)

    assert all(item.level is DiagnosticLevel.FAILURE for item in escalated)


def test_the_summary_reports_every_level() -> None:
    counted = summarize(
        [
            Diagnostic(
                level=DiagnosticLevel.EXPECTED_MISS,
                stage="discovery",
                code="http_status_error",
                message="404",
            )
        ]
    )

    assert counted == {
        "expected_miss": 1,
        "warning": 0,
        "failure": 0,
    }


# ---------------------------------------------------------------------------
# Navigation must not depend on a sitemap
# ---------------------------------------------------------------------------


def test_navigation_finds_the_documents_when_every_sitemap_is_missing(
    tmp_path: Path,
) -> None:
    """
    The investor section is followed even when all four guesses 404.

    This is the behaviour a full run appeared to contradict: every fund
    reported sitemap 404s, and it was not obvious from the report that
    the documents had been found by navigation anyway.
    """

    result = run_discovery(
        pages=NAVIGATION_ONLY_PAGES,
        tmp_path=tmp_path,
    )

    found = {document.document_type for document in result.documents}

    assert DocumentType.PRIIPS_KID in found

    assert DocumentType.STATUTE in found

    assert DocumentType.ANNUAL_REPORT in found

    assert result.is_sufficient

    urls = {document.url for document in result.documents}

    assert "https://www.maeg-investment.cz/dokumenty/statut-fondu.pdf" in urls


def test_the_missing_sitemaps_are_reported_as_expected_misses(
    tmp_path: Path,
) -> None:
    result = run_discovery(
        pages=NAVIGATION_ONLY_PAGES,
        tmp_path=tmp_path,
    )

    counted = summarize(list(result.diagnostics))

    # robots.txt and the three sitemap guesses all answered 404.
    assert counted["expected_miss"] >= 4

    assert counted["failure"] == 0

    # None of them reaches the warning lines a reader is shown.
    assert not [line for line in result.warnings if "sitemap" in line.lower()]

    assert not [line for line in result.warnings if "robots.txt" in line.lower()]


def test_a_real_server_error_still_reaches_the_warnings(
    tmp_path: Path,
) -> None:
    """Suppressing the guesses must not suppress a broken document link."""

    pages = dict(NAVIGATION_ONLY_PAGES)

    result = run_discovery(
        pages=pages,
        tmp_path=tmp_path,
        server_error_paths=frozenset({"/pro-investory/"}),
    )

    assert any("pro-investory" in line for line in result.warnings)


# ---------------------------------------------------------------------------
# The discovery log has to say what really became of a document
# ---------------------------------------------------------------------------


DUPLICATE_LINK_PAGES: dict[str, tuple[bytes, str]] = {
    "www.maeg-investment.cz/": (
        _html(
            """
            <a href="/pro-investory/">Pro investory</a>
            <a href="/for-investors/">For investors</a>
            """
        ),
        "text/html",
    ),
    # The Czech and the English page link the same factsheet. It is one
    # document seen twice, not one document and one duplicate.
    "www.maeg-investment.cz/pro-investory/": (
        _html('<a href="/files/factsheet-teaser.pdf">Fact Sheet</a>'),
        "text/html",
    ),
    "www.maeg-investment.cz/for-investors/": (
        _html('<a href="/files/factsheet-teaser.pdf">Fact Sheet</a>'),
        "text/html",
    ),
}


def test_a_document_linked_from_two_pages_is_logged_as_accepted(
    tmp_path: Path,
) -> None:
    """
    The decisive sighting is what the log must keep.

    The discovery log is keyed by address and upserts, so a later
    "duplicate, rejected" row used to overwrite the earlier accepted one.
    A factsheet that was found and downloaded then appeared in the log as
    refused, which made discovery look worse than it was.
    """

    result = run_discovery(
        pages=DUPLICATE_LINK_PAGES,
        tmp_path=tmp_path,
    )

    factsheet = "https://www.maeg-investment.cz/files/factsheet-teaser.pdf"

    assert factsheet in {document.url for document in result.documents}

    logged = [entry for entry in result.entries if entry.url == factsheet]

    assert len(logged) == 1, "one address, one entry"

    assert logged[0].accepted is True

    assert logged[0].rejection_reason is None
