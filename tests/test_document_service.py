from __future__ import annotations

import asyncio
from pathlib import Path

from fundscraper.database import (
    SourceStatus,
    get_database_status,
    initialize_database,
    list_parsed_documents,
    register_funds,
    upsert_source,
)
from fundscraper.document_service import (
    parse_fund_documents,
)
from fundscraper.models import FundInput
from fundscraper.output_service import (
    stable_fund_id,
)


def test_parses_downloaded_source_and_records_result(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com",
    )

    fund_id = stable_fund_id(fund)

    database_path = tmp_path / "fundscraper.sqlite3"

    source_path = tmp_path / "source.body"

    source_path.write_bytes(
        b"""
        <html>
          <body>
            <p>Investment horizon: 5 years</p>
          </body>
        </html>
        """
    )

    initialize_database(database_path)

    register_funds(
        database_path,
        [
            fund,
        ],
    )

    upsert_source(
        database_path,
        fund_id=fund_id,
        url="https://example.com/documents",
        status=SourceStatus.DOWNLOADED,
        document_type="marketing_page",
        content_type="text/html",
        http_status=200,
        sha256="a" * 64,
        local_path=str(source_path),
    )

    async def run_test() -> None:
        summary = await parse_fund_documents(
            database_path=database_path,
            fund=fund,
            parsed_directory=(tmp_path / "parsed"),
        )

        assert summary.sources_considered == 1
        assert summary.documents_parsed == 1
        assert summary.scanned_candidates == 0
        assert summary.total_characters > 0
        assert len(summary.failures) == 0

    asyncio.run(run_test())

    status = get_database_status(database_path)

    parsed_documents = list_parsed_documents(
        database_path,
        fund_id=fund_id,
    )

    assert status.parsed_documents_total == 1

    assert len(parsed_documents) == 1

    assert parsed_documents[0].document_format == "html"

    assert Path(parsed_documents[0].text_path).exists()
