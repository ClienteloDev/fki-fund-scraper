from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fundscraper.database import (
    AttemptStatus,
    SourceStatus,
    record_attempt,
    upsert_source,
)
from fundscraper.http_client import (
    FetchError,
    FetchResult,
    HttpFetcher,
)
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id


async def fetch_fund_start_page(
    *,
    database_path: Path,
    fund: FundInput,
    fetcher: HttpFetcher,
    force: bool = False,
) -> FetchResult:
    """Fetch and record the original website of one fund."""

    fund_id = stable_fund_id(fund)

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="fetch_start_page",
        status=AttemptStatus.STARTED,
        url=fund.web,
    )

    try:
        result = await fetcher.fetch(
            fund.web,
            force=force,
        )
    except FetchError as exc:
        failure_time = datetime.now(UTC)

        upsert_source(
            database_path,
            fund_id=fund_id,
            url=fund.web,
            status=SourceStatus.FAILED,
            document_type="marketing_page",
            retrieved_at=failure_time,
            error_code=exc.code,
            error_message=str(exc),
        )

        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="fetch_start_page",
            status=AttemptStatus.FAILED,
            url=fund.web,
            error_code=exc.code,
            error_message=str(exc),
            now=failure_time,
        )

        raise

    upsert_source(
        database_path,
        fund_id=fund_id,
        url=result.final_url,
        status=SourceStatus.DOWNLOADED,
        document_type="marketing_page",
        content_type=result.content_type,
        retrieved_at=result.retrieved_at,
        http_status=result.status_code,
        sha256=result.sha256,
        local_path=str(result.local_path),
    )

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="fetch_start_page",
        status=AttemptStatus.SUCCEEDED,
        url=result.final_url,
    )

    return result
