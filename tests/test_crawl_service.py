from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import (
    HttpSettings,
)
from fundscraper.crawl_service import (
    crawl_fund_site,
)
from fundscraper.database import (
    get_database_status,
    initialize_database,
    register_funds,
)
from fundscraper.http_client import (
    HttpFetcher,
)
from fundscraper.models import FundInput


def test_crawls_pages_and_downloads_documents(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/",
    )

    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [fund],
    )

    start_page = b"""
    <html>
      <head>
        <title>Example Fund</title>
      </head>
      <body>
        <a href="/pro-investory">
          Pro investory
        </a>

        <a href="/kontakt">
          Kontakt
        </a>
      </body>
    </html>
    """

    investor_page = b"""
    <html>
      <head>
        <title>Documents</title>
      </head>
      <body>
        <a href="/documents/kid.pdf">
          Klicove informace PRIIPs
        </a>

        <a href="/documents/statut.pdf">
          Statut fondu
        </a>
      </body>
    </html>
    """

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        path = request.url.path

        if path == "/":
            content = start_page
            content_type = "text/html"
        elif path == "/pro-investory":
            content = investor_page
            content_type = "text/html"
        elif path == "/documents/kid.pdf":
            content = b"%PDF-1.7 KID"
            content_type = "application/pdf"
        elif path == "/documents/statut.pdf":
            content = b"%PDF-1.7 STATUTE"
            content_type = "application/pdf"
        else:
            return httpx.Response(
                status_code=404,
                request=request,
            )

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": content_type,
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
            summary = await crawl_fund_site(
                database_path=database_path,
                fund=fund,
                fetcher=fetcher,
                max_pages=5,
                max_depth=2,
                max_documents=5,
            )

        assert summary.pages_visited == 2
        assert summary.html_pages_parsed == 2
        assert summary.documents_discovered == 2
        assert summary.documents_downloaded == 2
        assert len(summary.failures) == 0

    asyncio.run(run_test())

    status = get_database_status(database_path)

    assert status.funds_total == 1
    assert status.sources_total == 4
    assert status.attempts_total == 8


def test_downloads_adapter_seed_document(
    tmp_path: Path,
) -> None:
    from fundscraper.html_discovery import (
        DiscoveredLink,
    )
    from fundscraper.output_models import (
        DocumentType,
    )

    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/",
    )

    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [
            fund,
        ],
    )

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        if request.url.path == "/document.pdf":
            return httpx.Response(
                status_code=200,
                headers={
                    "Content-Type": "application/pdf",
                },
                content=b"%PDF-1.7 adapter document",
                request=request,
            )

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=b"<html><body>Fund</body></html>",
            request=request,
        )

    async def run_test() -> None:
        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        seed_document = DiscoveredLink(
            url="https://example.com/document.pdf",
            text="Klicove informace",
            score=150,
            document_type=(DocumentType.PRIIPS_KID),
            same_domain=True,
            direct_document=True,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            summary = await crawl_fund_site(
                database_path=database_path,
                fund=fund,
                fetcher=fetcher,
                max_pages=2,
                max_depth=1,
                max_documents=5,
                document_seed_links=(seed_document,),
            )

        assert summary.documents_discovered == 1

        assert summary.documents_downloaded == 1

    asyncio.run(run_test())
