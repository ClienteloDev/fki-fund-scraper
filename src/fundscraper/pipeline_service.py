from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from fundscraper.crawl_service import (
    CrawlSummary,
    crawl_fund_site,
)
from fundscraper.database import (
    AttemptStatus,
    DatabaseError,
    FundStatus,
    record_attempt,
    update_fund_status,
)
from fundscraper.document_service import (
    DocumentParsingSummary,
    parse_fund_documents,
)
from fundscraper.domain_adapters.base import (
    DomainAdapter,
    DomainAdapterResult,
)
from fundscraper.domain_adapters.registry import (
    get_amista_fallback_adapter,
    get_avant_fallback_adapter,
    get_domain_adapter,
    get_porovnejfondy_fallback_adapter,
)
from fundscraper.extraction_service import (
    ExtractionSummary,
    extract_fund_data,
)
from fundscraper.html_discovery import DiscoveredLink
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_url
from fundscraper.official_discovery import (
    OfficialDiscoveryResult,
    discover_official_sources,
)
from fundscraper.output_models import (
    ProcessingMetadata,
    ProcessingStatus,
)
from fundscraper.output_service import (
    OutputFileError,
    create_pending_output,
    load_output,
    stable_fund_id,
    write_output,
)


@dataclass(frozen=True, slots=True)
class FundPipelineResult:
    fund_id: str
    fund_name: str
    adapter_name: str | None
    status: FundStatus
    pages_visited: int
    documents_discovered: int
    documents_downloaded: int
    documents_parsed: int
    scanned_candidates: int
    fields_found: int
    fields_missing: int

    field_statuses: tuple[
        tuple[str, str],
        ...,
    ]

    failures: tuple[
        str,
        ...,
    ]

    duration_seconds: float

    # Reported next to the delivered fields, never inside them, so an
    # existing reader of this summary is unaffected by the new ones.
    extended_statuses: tuple[
        tuple[str, str],
        ...,
    ] = ()

    extended_rows: int = 0

    # What the official-source stage did before any external fallback
    # was allowed to run.
    official_pages_inspected: int = 0
    official_documents_found: int = 0
    official_documents_rejected: int = 0
    official_stage_exhausted: bool = False

    # Whether the official sources covered every required document
    # group. Completing the exploration and finding enough are separate
    # facts, and only the second one may skip a fallback.
    official_sources_sufficient: bool = False

    official_missing_document_groups: tuple[
        str,
        ...,
    ] = ()

    fallback_used: bool = False
    discovery_method_counts: tuple[
        tuple[str, int],
        ...,
    ] = ()


@dataclass(frozen=True, slots=True)
class BatchPipelineSummary:
    started_at: datetime
    finished_at: datetime
    output_path: Path

    results: tuple[
        FundPipelineResult,
        ...,
    ]

    @property
    def requested(self) -> int:
        return len(self.results)

    @property
    def completed(self) -> int:
        return sum(1 for result in self.results if result.status is FundStatus.COMPLETED)

    @property
    def partial(self) -> int:
        return sum(1 for result in self.results if result.status is FundStatus.PARTIAL)

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status is FundStatus.FAILED)


ProgressCallback = Callable[
    [
        int,
        int,
        FundPipelineResult,
    ],
    None,
]


async def _discover_adapter_sources(
    *,
    fund: FundInput,
    fetcher: HttpFetcher,
    force: bool,
    avant_fallback: bool,
    amista_fallback: bool,
    porovnejfondy_fallback: bool,
) -> tuple[DomainAdapterResult, str | None]:
    """Discover and merge explicit and fallback adapter sources."""

    adapters_by_name: dict[str, DomainAdapter] = {}

    explicit_adapter = get_domain_adapter(fund)

    if explicit_adapter is not None:
        adapters_by_name[explicit_adapter.name] = explicit_adapter

    if avant_fallback:
        adapter = get_avant_fallback_adapter()
        adapters_by_name.setdefault(adapter.name, adapter)

    if amista_fallback:
        adapter = get_amista_fallback_adapter()
        adapters_by_name.setdefault(adapter.name, adapter)

    if porovnejfondy_fallback:
        adapter = get_porovnejfondy_fallback_adapter()
        adapters_by_name.setdefault(adapter.name, adapter)

    navigation_by_url: dict[str, str] = {}
    documents_by_url: dict[str, DiscoveredLink] = {}
    warnings: list[str] = []
    active_adapter_names: list[str] = []

    for adapter in adapters_by_name.values():
        result = await adapter.discover(
            fund=fund,
            fetcher=fetcher,
            force=force,
        )

        warnings.extend(result.warnings)

        # Record adapters that were successfully attempted, even when
        # they did not find a matching page or document. This makes retry
        # diagnostics distinguish "fallback not used" from "fallback used
        # but no exact source was found".
        active_adapter_names.append(result.adapter_name)

        for navigation_url in result.navigation_urls:
            navigation_by_url[canonical_url(navigation_url)] = navigation_url

        for document in result.documents:
            key = canonical_url(document.url)
            existing = documents_by_url.get(key)

            if existing is None or document.score > existing.score:
                documents_by_url[key] = document

    adapter_name = "+".join(active_adapter_names) if active_adapter_names else None

    return (
        DomainAdapterResult(
            adapter_name=adapter_name or "none",
            navigation_urls=tuple(sorted(navigation_by_url.values())),
            documents=tuple(
                sorted(
                    documents_by_url.values(),
                    key=lambda item: (
                        -item.score,
                        item.url,
                    ),
                )
            ),
            warnings=tuple(warnings),
        ),
        adapter_name,
    )


def synchronize_output_file(
    *,
    funds: list[FundInput],
    output_path: Path,
    reset: bool = False,
) -> None:
    """
    Synchronize enriched output with the current input fund list.

    Existing results are preserved when their stable fund IDs
    still exist in the current input.
    """

    pending_outputs = create_pending_output(funds)

    if reset or not output_path.exists():
        write_output(
            output_path,
            pending_outputs,
            overwrite=output_path.exists(),
        )

        return

    existing_outputs = load_output(output_path)

    existing_by_id = {output.fund_id: output for output in existing_outputs}

    synchronized_outputs = [
        existing_by_id.get(
            pending_output.fund_id,
            pending_output,
        )
        for pending_output in pending_outputs
    ]

    write_output(
        output_path,
        synchronized_outputs,
        overwrite=True,
    )


async def run_fund_pipeline(
    *,
    database_path: Path,
    output_path: Path,
    parsed_directory: Path,
    fund: FundInput,
    fetcher: HttpFetcher,
    max_pages: int = 25,
    max_depth: int = 2,
    max_documents: int = 20,
    document_concurrency: int = 4,
    force: bool = False,
    avant_fallback: bool = False,
    amista_fallback: bool = False,
    porovnejfondy_fallback: bool = False,
    output_lock: asyncio.Lock | None = None,
    discovery_run_id: str = "",
) -> FundPipelineResult:
    """Run adapter discovery, crawl, parsing and extraction for one fund."""

    started = perf_counter()

    fund_id = stable_fund_id(fund)

    crawl_summary: CrawlSummary | None = None

    parsing_summary: DocumentParsingSummary | None = None

    extraction_summary: ExtractionSummary | None = None

    failures: list[str] = []

    adapter_name: str | None = None

    official_discovery: OfficialDiscoveryResult | None = None

    fallback_used = False

    adapter_result = DomainAdapterResult(
        adapter_name="none",
        navigation_urls=(),
        documents=(),
        warnings=(),
    )

    update_fund_status(
        database_path,
        fund_id=fund_id,
        status=FundStatus.IN_PROGRESS,
    )

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="pipeline",
        status=AttemptStatus.STARTED,
        url=fund.web,
    )

    try:
        # The official website of the fund is exhausted first. An
        # external source may only add what the fund itself does not
        # publish, so the adapters run afterwards and only if needed.
        official_result = await discover_official_sources(
            database_path=database_path,
            fund=fund,
            fetcher=fetcher,
            force=force,
            run_id=discovery_run_id,
        )

        official_discovery = official_result

        failures.extend(official_result.warnings)

        if official_result.is_sufficient:
            # The official site answered every required document group.
            # Running a third-party adapter now would only add weaker
            # copies of what the fund itself already published.
            adapter_result = DomainAdapterResult(
                adapter_name="official_only",
                navigation_urls=(),
                documents=(),
                warnings=(),
            )

            adapter_name = "official_only"
        else:
            # The official site was explored first and came up short.
            # What it does not publish is what the fallbacks are for.
            adapter_result, adapter_name = await _discover_adapter_sources(
                fund=fund,
                fetcher=fetcher,
                force=force,
                avant_fallback=avant_fallback,
                amista_fallback=amista_fallback,
                porovnejfondy_fallback=porovnejfondy_fallback,
            )

            fallback_used = bool(adapter_result.documents or adapter_result.navigation_urls)

            failures.extend(adapter_result.warnings)

        # This call must remain outside the adapter condition.
        # Funds without a domain adapter still need to run
        # through the normal crawler.
        crawl_result = await crawl_fund_site(
            database_path=database_path,
            fund=fund,
            fetcher=fetcher,
            max_pages=max_pages,
            max_depth=max_depth,
            max_documents=max_documents,
            document_concurrency=document_concurrency,
            navigation_seed_urls=(official_result.navigation_urls + adapter_result.navigation_urls),
            document_seed_links=(official_result.documents + adapter_result.documents),
            force=force,
        )

        # Keep an optional reference for the exception branch.
        crawl_summary = crawl_result

        failures.extend(
            (f"{failure.stage}: {failure.url}: {failure.error_code}: {failure.message}")
            for failure in crawl_result.failures
        )

        parsing_result = await parse_fund_documents(
            database_path=database_path,
            fund=fund,
            parsed_directory=parsed_directory,
            force=force,
        )

        parsing_summary = parsing_result

        failures.extend(
            (f"parse_document: {failure.url}: {failure.error_code}: {failure.message}")
            for failure in parsing_result.failures
        )

        if output_lock is None:
            extraction_result = await asyncio.to_thread(
                extract_fund_data,
                database_path=database_path,
                output_path=output_path,
                fund=fund,
            )
        else:
            async with output_lock:
                extraction_result = await asyncio.to_thread(
                    extract_fund_data,
                    database_path=database_path,
                    output_path=output_path,
                    fund=fund,
                )

        extraction_summary = extraction_result

        failures.extend(extraction_result.warnings)

        final_status = (
            FundStatus.COMPLETED if extraction_result.fields_found == 5 else FundStatus.PARTIAL
        )

        update_fund_status(
            database_path,
            fund_id=fund_id,
            status=final_status,
        )

        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="pipeline",
            status=AttemptStatus.SUCCEEDED,
            url=fund.web,
        )

        return FundPipelineResult(
            fund_id=fund_id,
            fund_name=fund.name,
            adapter_name=adapter_name,
            status=final_status,
            pages_visited=(crawl_result.pages_visited),
            documents_discovered=(crawl_result.documents_discovered),
            documents_downloaded=(crawl_result.documents_downloaded),
            documents_parsed=(parsing_result.documents_parsed),
            scanned_candidates=(parsing_result.scanned_candidates),
            fields_found=(extraction_result.fields_found),
            fields_missing=(extraction_result.fields_missing),
            field_statuses=tuple(
                (
                    field_name,
                    status.value,
                )
                for field_name, status in extraction_result.field_statuses
            ),
            extended_statuses=tuple(
                (
                    field_name,
                    status.value,
                )
                for field_name, status in extraction_result.extended_statuses
            ),
            extended_rows=extraction_result.extended_rows,
            official_pages_inspected=(official_result.metrics.pages_inspected),
            official_documents_found=(official_result.metrics.documents_accepted),
            official_documents_rejected=(official_result.metrics.documents_rejected),
            official_stage_exhausted=(official_result.is_exhausted),
            official_sources_sufficient=(official_result.is_sufficient),
            official_missing_document_groups=(official_result.missing_document_groups),
            fallback_used=fallback_used,
            discovery_method_counts=tuple(sorted(official_result.metrics.method_counts.items())),
            failures=tuple(failures),
            duration_seconds=round(
                perf_counter() - started,
                3,
            ),
        )

    except Exception as exc:
        failure_message = f"{type(exc).__name__}: {exc}"

        failures.append(failure_message)

        with suppress(DatabaseError):
            update_fund_status(
                database_path,
                fund_id=fund_id,
                status=FundStatus.FAILED,
            )

        with suppress(DatabaseError):
            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="pipeline",
                status=AttemptStatus.FAILED,
                url=fund.web,
                error_code="pipeline_error",
                error_message=failure_message,
            )

        if output_lock is None:
            with suppress(OutputFileError):
                _mark_output_failed(
                    output_path=output_path,
                    fund_id=fund_id,
                    failure_message=failure_message,
                )
        else:
            async with output_lock:
                with suppress(OutputFileError):
                    _mark_output_failed(
                        output_path=output_path,
                        fund_id=fund_id,
                        failure_message=failure_message,
                    )

        return FundPipelineResult(
            fund_id=fund_id,
            fund_name=fund.name,
            adapter_name=adapter_name,
            status=FundStatus.FAILED,
            pages_visited=(crawl_summary.pages_visited if crawl_summary is not None else 0),
            documents_discovered=(
                crawl_summary.documents_discovered if crawl_summary is not None else 0
            ),
            documents_downloaded=(
                crawl_summary.documents_downloaded if crawl_summary is not None else 0
            ),
            documents_parsed=(
                parsing_summary.documents_parsed if parsing_summary is not None else 0
            ),
            scanned_candidates=(
                parsing_summary.scanned_candidates if parsing_summary is not None else 0
            ),
            official_pages_inspected=(
                official_discovery.metrics.pages_inspected if official_discovery is not None else 0
            ),
            official_documents_found=(
                official_discovery.metrics.documents_accepted
                if official_discovery is not None
                else 0
            ),
            official_documents_rejected=(
                official_discovery.metrics.documents_rejected
                if official_discovery is not None
                else 0
            ),
            official_stage_exhausted=(
                official_discovery.is_exhausted if official_discovery is not None else False
            ),
            official_sources_sufficient=(
                official_discovery.is_sufficient if official_discovery is not None else False
            ),
            official_missing_document_groups=(
                official_discovery.missing_document_groups if official_discovery is not None else ()
            ),
            fallback_used=fallback_used,
            fields_found=(extraction_summary.fields_found if extraction_summary is not None else 0),
            fields_missing=(
                extraction_summary.fields_missing if extraction_summary is not None else 5
            ),
            field_statuses=(
                tuple(
                    (
                        field_name,
                        status.value,
                    )
                    for field_name, status in extraction_summary.field_statuses
                )
                if extraction_summary is not None
                else ()
            ),
            failures=tuple(failures),
            duration_seconds=round(
                perf_counter() - started,
                3,
            ),
        )


async def run_fund_batch(
    *,
    database_path: Path,
    output_path: Path,
    parsed_directory: Path,
    funds: list[FundInput],
    fetcher: HttpFetcher,
    max_pages: int = 25,
    max_depth: int = 2,
    max_documents: int = 20,
    document_concurrency: int = 4,
    concurrency: int = 6,
    force: bool = False,
    avant_fallback: bool = False,
    amista_fallback: bool = False,
    porovnejfondy_fallback: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> BatchPipelineSummary:
    """Run multiple funds concurrently with serialized output writes."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least one")

    started_at = datetime.now(UTC)
    total = len(funds)
    fund_semaphore = asyncio.Semaphore(concurrency)
    output_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    completed_count = 0

    indexed_results: list[FundPipelineResult | None] = [None] * total

    async def run_one(
        index: int,
        fund: FundInput,
    ) -> None:
        nonlocal completed_count

        async with fund_semaphore:
            result = await run_fund_pipeline(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=parsed_directory,
                fund=fund,
                fetcher=fetcher,
                max_pages=max_pages,
                max_depth=max_depth,
                max_documents=max_documents,
                document_concurrency=document_concurrency,
                force=force,
                avant_fallback=avant_fallback,
                amista_fallback=amista_fallback,
                porovnejfondy_fallback=porovnejfondy_fallback,
                output_lock=output_lock,
            )

        indexed_results[index] = result

        async with progress_lock:
            completed_count += 1

            if progress_callback is not None:
                progress_callback(
                    completed_count,
                    total,
                    result,
                )

    await asyncio.gather(*(run_one(index, fund) for index, fund in enumerate(funds)))

    results = tuple(result for result in indexed_results if result is not None)

    if len(results) != total:
        raise RuntimeError(
            f"Concurrent fund batch did not produce all results: {len(results)} != {total}"
        )

    return BatchPipelineSummary(
        started_at=started_at,
        finished_at=datetime.now(UTC),
        output_path=output_path,
        results=results,
    )


def write_batch_report(
    *,
    summary: BatchPipelineSummary,
    report_path: Path,
) -> None:
    """Write a machine-readable report for a batch pipeline run."""

    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "started_at": (summary.started_at.isoformat()),
        "finished_at": (summary.finished_at.isoformat()),
        "output_path": str(summary.output_path),
        "requested": summary.requested,
        "completed": summary.completed,
        "partial": summary.partial,
        "failed": summary.failed,
        "results": [
            {
                "fund_id": result.fund_id,
                "fund_name": result.fund_name,
                "adapter_name": (result.adapter_name),
                "status": result.status.value,
                "pages_visited": (result.pages_visited),
                "documents_discovered": (result.documents_discovered),
                "documents_downloaded": (result.documents_downloaded),
                "documents_parsed": (result.documents_parsed),
                "scanned_candidates": (result.scanned_candidates),
                "fields_found": (result.fields_found),
                "fields_missing": (result.fields_missing),
                "field_statuses": {
                    field_name: status for field_name, status in result.field_statuses
                },
                "failures": list(result.failures),
                "duration_seconds": (result.duration_seconds),
            }
            for result in summary.results
        ],
    }

    temporary_path = report_path.with_suffix(f"{report_path.suffix}.tmp")

    try:
        temporary_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(report_path)
    except OSError:
        temporary_path.unlink(missing_ok=True)

        raise


def _mark_output_failed(
    *,
    output_path: Path,
    fund_id: str,
    failure_message: str,
) -> None:
    """Mark one output fund as failed without replacing extracted fields."""

    outputs = load_output(output_path)

    matching_index = next(
        (index for index, output in enumerate(outputs) if output.fund_id == fund_id),
        None,
    )

    if matching_index is None:
        raise OutputFileError(f"Failed pipeline fund does not exist in output file: {fund_id}")

    current_output = outputs[matching_index]

    warnings = list(current_output.processing.warnings)

    warnings.append(failure_message)

    outputs[matching_index] = current_output.model_copy(
        update={
            "processing": ProcessingMetadata(
                status=ProcessingStatus.FAILED,
                updated_at=datetime.now(UTC),
                warnings=warnings,
            )
        }
    )

    write_output(
        output_path,
        outputs,
        overwrite=True,
    )
