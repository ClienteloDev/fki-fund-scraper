from __future__ import annotations

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
    DomainAdapterResult,
)
from fundscraper.domain_adapters.registry import (
    get_domain_adapter,
)
from fundscraper.extraction_service import (
    ExtractionSummary,
    extract_fund_data,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
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
    force: bool = False,
) -> FundPipelineResult:
    """Run adapter discovery, crawl, parsing and extraction for one fund."""

    started = perf_counter()

    fund_id = stable_fund_id(fund)

    crawl_summary: CrawlSummary | None = None

    parsing_summary: DocumentParsingSummary | None = None

    extraction_summary: ExtractionSummary | None = None

    failures: list[str] = []

    adapter_name: str | None = None

    adapter_result = DomainAdapterResult(
        adapter_name="none",
        navigation_urls=(),
        documents=(),
        warnings=(),
    )

    adapter = get_domain_adapter(fund)

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
        if adapter is not None:
            adapter_result = await adapter.discover(
                fund=fund,
                fetcher=fetcher,
                force=force,
            )

            adapter_name = adapter_result.adapter_name

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
            navigation_seed_urls=(adapter_result.navigation_urls),
            document_seed_links=(adapter_result.documents),
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

        extraction_result = extract_fund_data(
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
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> BatchPipelineSummary:
    """Run the complete pipeline sequentially for multiple funds."""

    started_at = datetime.now(UTC)

    results: list[FundPipelineResult] = []

    total = len(funds)

    for index, fund in enumerate(
        funds,
        start=1,
    ):
        result = await run_fund_pipeline(
            database_path=database_path,
            output_path=output_path,
            parsed_directory=parsed_directory,
            fund=fund,
            fetcher=fetcher,
            max_pages=max_pages,
            max_depth=max_depth,
            max_documents=max_documents,
            force=force,
        )

        results.append(result)

        if progress_callback is not None:
            progress_callback(
                index,
                total,
                result,
            )

    return BatchPipelineSummary(
        started_at=started_at,
        finished_at=datetime.now(UTC),
        output_path=output_path,
        results=tuple(results),
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
