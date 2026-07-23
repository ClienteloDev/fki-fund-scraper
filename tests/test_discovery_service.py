from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.database import (
    get_database_status,
    initialize_database,
    register_funds,
)
from fundscraper.discovery_service import (
    discover_fund_start_page,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput


def test_discovers_and_records_document_sources(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/fund",
    )

    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [fund],
    )

    html = b"""
    <!doctype html>
    <html>
      <head>
        <title>Example Fund</title>
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
        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html",
            },
            content=html,
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
            summary = await discover_fund_start_page(
                database_path=database_path,
                fund=fund,
                fetcher=fetcher,
            )

        assert summary.page_title == "Example Fund"
        assert len(summary.candidates) == 2

    asyncio.run(run_test())

    status = get_database_status(database_path)

    assert status.funds_total == 1
    assert status.sources_total == 3
    assert status.attempts_total == 4
