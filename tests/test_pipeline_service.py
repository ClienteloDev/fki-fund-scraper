from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from fundscraper.config import HttpSettings
from fundscraper.database import (
    FundStatus,
    get_database_status,
    initialize_database,
    register_funds,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.output_models import (
    FieldStatus,
)
from fundscraper.output_service import (
    load_output,
)
from fundscraper.pipeline_service import (
    run_fund_batch,
    run_fund_pipeline,
    synchronize_output_file,
    write_batch_report,
)


def test_runs_complete_pipeline_for_one_fund(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/",
    )

    database_path = tmp_path / "fundscraper.sqlite3"

    output_path = tmp_path / "funds.enriched.json"

    report_path = tmp_path / "sample-run.json"

    initialize_database(database_path)

    register_funds(
        database_path,
        [
            fund,
        ],
    )

    synchronize_output_file(
        funds=[
            fund,
        ],
        output_path=output_path,
    )

    html = b"""
    <!doctype html>
    <html>
      <head>
        <title>Example Fund</title>
      </head>
      <body>
        <p>Doporuceny investicni horizont je 5 let.</p>
        <p>Minimalni investice cini 1 000 000 Kc.</p>
        <p>Cilovy vynos fondu je 8 % p.a.</p>
        <p>Vstupni poplatek cini 2 %.</p>
        <p>
          Hodnota majetku fondu k 31. 12. 2025
          cinila 500 mil. Kc.
        </p>
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
            result = await run_fund_pipeline(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=(tmp_path / "parsed"),
                fund=fund,
                fetcher=fetcher,
                max_pages=5,
                max_depth=1,
                max_documents=5,
            )

            assert result.status is FundStatus.COMPLETED

            assert result.fields_found == 5
            assert result.fields_missing == 0

            batch_summary = await run_fund_batch(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=(tmp_path / "parsed"),
                funds=[
                    fund,
                ],
                fetcher=fetcher,
                max_pages=5,
                max_depth=1,
                max_documents=5,
            )

        write_batch_report(
            summary=batch_summary,
            report_path=report_path,
        )

    asyncio.run(run_test())

    output = load_output(output_path)

    assert len(output) == 1

    assert output[0].investment_horizon.status is FieldStatus.FOUND

    assert output[0].minimum_investment.status is FieldStatus.FOUND

    assert output[0].target_return.status is FieldStatus.FOUND

    assert output[0].fees.status is FieldStatus.FOUND

    assert output[0].assets_under_management.status is FieldStatus.FOUND

    database_status = get_database_status(database_path)

    assert database_status.funds_completed == 1

    report_payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert report_payload["requested"] == 1

    assert report_payload["completed"] == 1

    assert report_payload["failed"] == 0


def test_pipeline_can_use_avant_as_explicit_fallback(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/",
    )

    database_path = tmp_path / "fundscraper.sqlite3"
    output_path = tmp_path / "funds.enriched.json"

    initialize_database(database_path)
    register_funds(
        database_path,
        [fund],
    )
    synchronize_output_file(
        funds=[fund],
        output_path=output_path,
    )

    html = b"""
    <!doctype html>
    <html>
      <body>
        <h1>Example SICAV a.s.</h1>
        <p>Doporuceny investicni horizont je 5 let.</p>
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
            result = await run_fund_pipeline(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=(tmp_path / "parsed"),
                fund=fund,
                fetcher=fetcher,
                max_pages=2,
                max_depth=0,
                max_documents=0,
                avant_fallback=True,
            )

        assert result.adapter_name == "avantfunds"

    asyncio.run(run_test())


def test_batch_processes_multiple_funds_concurrently(
    tmp_path: Path,
) -> None:
    funds = [
        FundInput(
            name=f"Example {index} SICAV a.s.",
            web=f"https://fund-{index}.example/",
        )
        for index in range(3)
    ]
    database_path = tmp_path / "fundscraper.sqlite3"
    output_path = tmp_path / "funds.enriched.json"

    initialize_database(database_path)
    register_funds(database_path, funds)
    synchronize_output_file(
        funds=funds,
        output_path=output_path,
    )

    active_requests = 0
    maximum_active_requests = 0

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal active_requests, maximum_active_requests

        active_requests += 1
        maximum_active_requests = max(
            maximum_active_requests,
            active_requests,
        )
        await asyncio.sleep(0.05)
        fund_number = str(request.url.host).split("-")[1].split(".")[0]
        html = f"""
        <html><body>
          <h1>Example {fund_number} SICAV a.s.</h1>
          <p>Doporuceny investicni horizont je 5 let.</p>
        </body></html>
        """.encode()
        active_requests -= 1

        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/html"},
            content=html,
            request=request,
        )

    async def run_test() -> None:
        settings = HttpSettings(
            max_retries=0,
            max_concurrency=10,
            max_per_domain_concurrency=1,
            requests_per_second=100,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=httpx.MockTransport(handler),
        ) as fetcher:
            summary = await run_fund_batch(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=tmp_path / "parsed",
                funds=funds,
                fetcher=fetcher,
                max_pages=1,
                max_depth=0,
                max_documents=0,
                concurrency=3,
            )

        assert summary.requested == 3

    asyncio.run(run_test())

    assert maximum_active_requests >= 2

    output = load_output(output_path)
    assert len(output) == 3
    assert all(item.investment_horizon.status is FieldStatus.FOUND for item in output)
