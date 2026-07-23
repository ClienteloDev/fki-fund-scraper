from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.domain_adapters.avant import (
    AvantFundsAdapter,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.output_models import (
    DocumentType,
)


def test_discovers_only_exact_avant_fund_documents(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="AVANT Finance SICAV a. s.",
        web=("https://www.avantfunds.cz/investice-do-fondu/"),
    )

    catalog_html = b"""
    <!doctype html>
    <html>
    <body>
        <section>
        <h2>AVANT Finance SICAV a. s.</h2>

        <a href="/fondy/avant-finance-sicav-a-s/">
            Detail fondu AVANT Finance
        </a>

        <a href="/wp-content/uploads/fondy/avant-finance-sicav-a-s/kid.pdf">
            Sdeleni klicovych informaci
        </a>

        <a href="/wp-content/uploads/fondy/avant-finance-sicav-a-s/vz-2025.pdf">
            Vyrocni zprava 2025
        </a>

        <a href="/wp-content/uploads/fondy/avant-finance-sicav-a-s/pozvanka.pdf">
            Pozvanka na valnou hromadu
        </a>
        </section>

        <section>
        <h2>Other Fund SICAV a.s.</h2>

        <a href="/wp-content/uploads/fondy/other-fund/kid.pdf">
            Sdeleni klicovych informaci
        </a>
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
            content=catalog_html,
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
            result = await AvantFundsAdapter().discover(
                fund=fund,
                fetcher=fetcher,
            )

        assert result.adapter_name == "avantfunds"

        assert len(result.navigation_urls) == 1

        assert result.navigation_urls[0] == (
            "https://www.avantfunds.cz/fondy/avant-finance-sicav-a-s/"
        )

        assert len(result.documents) == 2

        document_types = {document.document_type for document in result.documents}

        assert DocumentType.PRIIPS_KID in document_types

        assert DocumentType.ANNUAL_REPORT in document_types

        document_urls = {document.url for document in result.documents}

        assert not any("other-fund" in url for url in document_urls)

        assert not any("pozvanka" in url for url in document_urls)

    asyncio.run(run_test())
