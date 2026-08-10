from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fundscraper.database import (
    AttemptStatus,
    SourceStatus,
    record_attempt,
    upsert_source,
)
from fundscraper.html_discovery import (
    DiscoveredLink,
    HtmlDiscoveryError,
    is_html_response,
    parse_html_page,
)
from fundscraper.http_client import (
    FetchError,
    HttpFetcher,
)
from fundscraper.models import FundInput
from fundscraper.normalization import (
    canonical_url,
)
from fundscraper.output_models import (
    DocumentType,
)
from fundscraper.output_service import (
    stable_fund_id,
)
from fundscraper.site_crawler import (
    discover_navigation_links,
)


class CrawlError(RuntimeError):
    """Raised when crawler configuration is invalid."""


@dataclass(frozen=True, slots=True)
class CrawlFailure:
    url: str
    stage: str
    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class CrawlSummary:
    fund_id: str
    fund_name: str
    pages_visited: int
    html_pages_parsed: int
    documents_discovered: int
    documents_downloaded: int
    failures: tuple[CrawlFailure, ...]
    document_candidates: tuple[DiscoveredLink, ...]


async def crawl_fund_site(
    *,
    database_path: Path,
    fund: FundInput,
    fetcher: HttpFetcher,
    max_pages: int = 25,
    max_depth: int = 2,
    max_documents: int = 20,
    document_concurrency: int = 4,
    navigation_seed_urls: tuple[str, ...] = (),
    document_seed_links: tuple[DiscoveredLink, ...] = (),
    force: bool = False,
) -> CrawlSummary:
    """Crawl selected HTML pages and download discovered documents."""

    if max_pages < 1:
        raise CrawlError("max_pages must be at least one")

    if max_depth < 0:
        raise CrawlError("max_depth must not be negative")

    if max_documents < 0:
        raise CrawlError("max_documents must not be negative")

    if document_concurrency < 1:
        raise CrawlError("document_concurrency must be at least one")

    fund_id = stable_fund_id(fund)

    queue: deque[tuple[str, int]] = deque()

    queued_urls: set[str] = set()

    seed_candidates = (fund.web, *navigation_seed_urls) if fund.web else navigation_seed_urls

    for seed_url in seed_candidates:
        seed_key = canonical_url(seed_url)

        if seed_key in queued_urls:
            continue

        queued_urls.add(seed_key)

        queue.append(
            (
                seed_url,
                0,
            )
        )

    visited_pages: set[str] = set()

    documents_by_url: dict[
        str,
        DiscoveredLink,
    ] = {}

    for document in document_seed_links:
        document_key = canonical_url(document.url)

        existing_document = documents_by_url.get(document_key)

        if existing_document is None or document.score > existing_document.score:
            documents_by_url[document_key] = document

        upsert_source(
            database_path,
            fund_id=fund_id,
            url=document.url,
            status=SourceStatus.DISCOVERED,
            document_type=(document.document_type.value),
            title=(document.text or None),
        )

    failures: list[CrawlFailure] = []

    pages_visited = 0
    html_pages_parsed = 0

    while queue and pages_visited < max_pages:
        requested_url, depth = queue.popleft()

        requested_key = canonical_url(requested_url)

        if requested_key in visited_pages:
            continue

        visited_pages.add(requested_key)

        attempt_time = datetime.now(UTC)

        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="crawl_page",
            status=AttemptStatus.STARTED,
            url=requested_url,
            now=attempt_time,
        )

        try:
            result = await fetcher.fetch(
                requested_url,
                force=force,
            )
        except FetchError as exc:
            failures.append(
                CrawlFailure(
                    url=requested_url,
                    stage="crawl_page",
                    error_code=exc.code,
                    message=str(exc),
                )
            )

            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="crawl_page",
                status=AttemptStatus.FAILED,
                url=requested_url,
                error_code=exc.code,
                error_message=str(exc),
            )

            continue

        pages_visited += 1

        upsert_source(
            database_path,
            fund_id=fund_id,
            url=result.final_url,
            status=SourceStatus.DOWNLOADED,
            document_type=(DocumentType.MARKETING_PAGE.value),
            content_type=result.content_type,
            retrieved_at=result.retrieved_at,
            http_status=result.status_code,
            sha256=result.sha256,
            local_path=str(result.local_path),
        )

        if not is_html_response(
            content_type=result.content_type,
            body=result.body,
        ):
            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="crawl_page",
                status=AttemptStatus.SKIPPED,
                url=result.final_url,
                error_code="not_html",
                error_message=("Crawled page did not contain HTML"),
            )

            continue

        try:
            parsed_page = parse_html_page(
                body=result.body,
                page_url=result.final_url,
            )
        except HtmlDiscoveryError as exc:
            failures.append(
                CrawlFailure(
                    url=result.final_url,
                    stage="parse_html",
                    error_code=exc.code,
                    message=str(exc),
                )
            )

            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="crawl_page",
                status=AttemptStatus.FAILED,
                url=result.final_url,
                error_code=exc.code,
                error_message=str(exc),
            )

            continue

        html_pages_parsed += 1

        for candidate in parsed_page.candidates:
            if not candidate.direct_document:
                continue

            candidate_key = canonical_url(candidate.url)

            existing = documents_by_url.get(candidate_key)

            if existing is None or candidate.score > existing.score:
                documents_by_url[candidate_key] = candidate

            upsert_source(
                database_path,
                fund_id=fund_id,
                url=candidate.url,
                status=SourceStatus.DISCOVERED,
                document_type=(candidate.document_type.value),
                title=(candidate.text or None),
            )

        if depth < max_depth:
            navigation_links = discover_navigation_links(
                body=result.body,
                page_url=result.final_url,
                effective_base_url=(parsed_page.effective_base_url),
                fund_name=fund.name,
            )

            for navigation_link in navigation_links:
                navigation_key = canonical_url(navigation_link.url)

                if navigation_key in visited_pages:
                    continue

                queue.append(
                    (
                        navigation_link.url,
                        depth + 1,
                    )
                )

        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="crawl_page",
            status=AttemptStatus.SUCCEEDED,
            url=result.final_url,
        )

    ordered_documents = tuple(
        sorted(
            documents_by_url.values(),
            key=lambda item: (
                -item.score,
                item.url,
            ),
        )
    )

    download_semaphore = asyncio.Semaphore(document_concurrency)

    async def download_document(
        document: DiscoveredLink,
    ) -> tuple[bool, CrawlFailure | None]:
        async with download_semaphore:
            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="download_document",
                status=AttemptStatus.STARTED,
                url=document.url,
            )

            try:
                result = await fetcher.fetch(
                    document.url,
                    force=force,
                )
            except FetchError as exc:
                upsert_source(
                    database_path,
                    fund_id=fund_id,
                    url=document.url,
                    status=SourceStatus.FAILED,
                    document_type=(document.document_type.value),
                    title=(document.text or None),
                    error_code=exc.code,
                    error_message=str(exc),
                )

                record_attempt(
                    database_path,
                    fund_id=fund_id,
                    stage="download_document",
                    status=AttemptStatus.FAILED,
                    url=document.url,
                    error_code=exc.code,
                    error_message=str(exc),
                )

                return (
                    False,
                    CrawlFailure(
                        url=document.url,
                        stage="download_document",
                        error_code=exc.code,
                        message=str(exc),
                    ),
                )

            upsert_source(
                database_path,
                fund_id=fund_id,
                url=result.final_url,
                status=SourceStatus.DOWNLOADED,
                document_type=(document.document_type.value),
                content_type=result.content_type,
                title=(document.text or None),
                retrieved_at=result.retrieved_at,
                http_status=result.status_code,
                sha256=result.sha256,
                local_path=str(result.local_path),
            )

            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="download_document",
                status=AttemptStatus.SUCCEEDED,
                url=result.final_url,
            )

            return (True, None)

    download_results = await asyncio.gather(
        *(download_document(document) for document in ordered_documents[:max_documents])
    )

    documents_downloaded = sum(1 for succeeded, _ in download_results if succeeded)

    failures.extend(failure for _, failure in download_results if failure is not None)

    return CrawlSummary(
        fund_id=fund_id,
        fund_name=fund.name,
        pages_visited=pages_visited,
        html_pages_parsed=html_pages_parsed,
        documents_discovered=len(ordered_documents),
        documents_downloaded=(documents_downloaded),
        failures=tuple(failures),
        document_candidates=(ordered_documents),
    )
