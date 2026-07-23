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
