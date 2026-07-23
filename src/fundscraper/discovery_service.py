from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fundscraper.database import (
    AttemptStatus,
    SourceStatus,
    record_attempt,
    upsert_source,
)
from fundscraper.fetch_service import (
    fetch_fund_start_page,
)
from fundscraper.html_discovery import (
    DiscoveredLink,
    HtmlDiscoveryError,
    is_html_response,
    parse_html_page,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id


@dataclass(frozen=True, slots=True)
class DiscoverySummary:
    fund_id: str
    fund_name: str
    page_url: str
    page_title: str | None
    links_total: int
    candidates: tuple[DiscoveredLink, ...]


async def discover_fund_start_page(
    *,
    database_path: Path,
    fund: FundInput,
    fetcher: HttpFetcher,
    force: bool = False,
) -> DiscoverySummary:
    """Download one fund page and discover relevant document links."""

    fund_id = stable_fund_id(fund)

    result = await fetch_fund_start_page(
        database_path=database_path,
        fund=fund,
        fetcher=fetcher,
        force=force,
    )

    discovery_time = datetime.now(UTC)

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="discover_start_page",
        status=AttemptStatus.STARTED,
        url=result.final_url,
        now=discovery_time,
    )

    try:
        if not is_html_response(
            content_type=result.content_type,
            body=result.body,
        ):
            raise HtmlDiscoveryError("Start page response is not HTML")

        parsed_page = parse_html_page(
            body=result.body,
            page_url=result.final_url,
        )

        for candidate in parsed_page.candidates:
            upsert_source(
                database_path,
                fund_id=fund_id,
                url=candidate.url,
                status=SourceStatus.DISCOVERED,
                document_type=(candidate.document_type.value),
                title=(candidate.text or None),
                now=discovery_time,
            )
    except HtmlDiscoveryError as exc:
        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="discover_start_page",
            status=AttemptStatus.FAILED,
            url=result.final_url,
            error_code=exc.code,
            error_message=str(exc),
            now=discovery_time,
        )

        raise

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="discover_start_page",
        status=AttemptStatus.SUCCEEDED,
        url=result.final_url,
        now=discovery_time,
    )

    return DiscoverySummary(
        fund_id=fund_id,
        fund_name=fund.name,
        page_url=result.final_url,
        page_title=parsed_page.title,
        links_total=parsed_page.links_total,
        candidates=parsed_page.candidates,
    )
