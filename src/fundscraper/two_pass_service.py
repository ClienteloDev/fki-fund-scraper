"""
The two-pass crawl: a cheap look at every fund, a deep one where needed.

The first pass gives every fund a small budget and stops as soon as the
official site has answered the document groups a fund is expected to
publish. Most funds are finished there. The second pass takes only the
funds that came back with a field missing, refused or contested, gives
them a much larger budget, and tells the discovery which document types
would answer their particular question.

Both passes run the existing official-first discovery and the existing
crawler. Nothing here downloads or parses anything itself; it decides who
goes out, how far, and what for, and it writes down what happened.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The delivered fields a recovery is measured on. News is excluded for
# the same reason it never triggers a deep pass.
from fundscraper.crawl_planning import (
    DEEP_BUDGET,
    FAST_BUDGET,
    TRIGGER_FIELDS,
    CrawlBudget,
    CrawlPass,
    DeepPassPlan,
    DiscoveryOutcome,
    select_deep_pass_funds,
)
from fundscraper.database import initialize_database, register_funds
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.output_service import load_output, stable_fund_id
from fundscraper.pipeline_service import (
    FundPipelineResult,
    run_fund_pipeline,
    synchronize_output_file,
)
from fundscraper.run_diagnostics import DiagnosticLevel

# Called after each fund with the pass, the position, the total and the
# result, so a caller can print a line without the service knowing how.
type TwoPassProgress = Callable[
    [
        CrawlPass,
        int,
        int,
        FundPipelineResult,
    ],
    None,
]


@dataclass(frozen=True, slots=True)
class PassMetrics:
    """What one pass cost and what it produced."""

    funds: int = 0
    pages_visited: int = 0
    documents_discovered: int = 0
    documents_downloaded: int = 0
    documents_parsed: int = 0
    superseded_documents: int = 0
    failures: int = 0
    duration_seconds: float = 0.0

    expected_misses: int = 0
    warnings: int = 0
    real_failures: int = 0

    discovery_seconds: float = 0.0
    crawl_seconds: float = 0.0
    parse_seconds: float = 0.0
    extract_seconds: float = 0.0

    @classmethod
    def of(
        cls,
        results: Sequence[FundPipelineResult],
    ) -> PassMetrics:
        return cls(
            funds=len(results),
            pages_visited=sum(item.pages_visited for item in results),
            documents_discovered=sum(item.documents_discovered for item in results),
            documents_downloaded=sum(item.documents_downloaded for item in results),
            documents_parsed=sum(item.documents_parsed for item in results),
            superseded_documents=sum(item.superseded_documents for item in results),
            failures=sum(len(item.failures) for item in results),
            duration_seconds=round(
                sum(item.duration_seconds for item in results),
                3,
            ),
            expected_misses=sum(item.expected_misses for item in results),
            warnings=sum(item.diagnostic_counts[DiagnosticLevel.WARNING.value] for item in results),
            real_failures=sum(item.real_failures for item in results),
            discovery_seconds=round(sum(item.timings.discovery for item in results), 1),
            crawl_seconds=round(sum(item.timings.crawl for item in results), 1),
            parse_seconds=round(sum(item.timings.parse for item in results), 1),
            extract_seconds=round(sum(item.timings.extract for item in results), 1),
        )


@dataclass
class TwoPassSummary:
    """Everything the run did, in the shape the report is written from."""

    started_at: datetime
    finished_at: datetime
    output_path: Path

    fast_results: list[FundPipelineResult] = dataclass_field(default_factory=list)
    deep_results: list[FundPipelineResult] = dataclass_field(default_factory=list)
    plans: list[DeepPassPlan] = dataclass_field(default_factory=list)

    # Field states before and after the deep pass, per fund, so a
    # recovered field can be named rather than only counted.
    recovered_fields: list[tuple[str, str, str, str]] = dataclass_field(default_factory=list)

    canonical_funds: int = 0

    @property
    def fast_metrics(self) -> PassMetrics:
        return PassMetrics.of(self.fast_results)

    @property
    def deep_metrics(self) -> PassMetrics:
        return PassMetrics.of(self.deep_results)


def field_states(
    records: Sequence[Any],
) -> dict[str, dict[str, str]]:
    """Return the status of every tracked field, keyed by fund."""

    states: dict[str, dict[str, str]] = {}

    for record in records:
        states[record.fund_id] = {
            name: getattr(record, name).status.value
            for name in TRIGGER_FIELDS
            if hasattr(record, name)
        }

    return states


async def run_two_pass(
    *,
    database_path: Path,
    output_path: Path,
    parsed_directory: Path,
    canonical_funds: list[FundInput],
    selected_funds: list[FundInput],
    fetcher: HttpFetcher,
    audit_findings: Sequence[dict[str, Any]] = (),
    conflict_records: Sequence[dict[str, Any]] = (),
    fast_budget: CrawlBudget = FAST_BUDGET,
    deep_budget: CrawlBudget = DEEP_BUDGET,
    concurrency: int = 6,
    force: bool = False,
    avant_fallback: bool = False,
    amista_fallback: bool = False,
    porovnejfondy_fallback: bool = False,
    anydoc_fallback: bool = False,
    run_id: str = "step7",
    # Compute and report the deep-pass selection without crawling it.
    skip_deep_pass: bool = False,
    # Crawl at most this many funds in the deep pass, highest priority
    # first. Zero means every selected fund.
    deep_limit: int = 0,
    progress: TwoPassProgress | None = None,
) -> TwoPassSummary:
    """
    Run the fast pass over the selection, then the deep pass where needed.

    ``canonical_funds`` is always the whole input list and ``selected_
    funds`` is what this run crawls. The database and the output file are
    synchronized against the canonical list first, so running a sample
    never shrinks the dataset to the sample.

    ``skip_deep_pass`` still selects the funds that would be crawled again
    and reports them, but crawls none of them. It is what makes a dry run
    of the selection cheap.
    """

    started_at = datetime.now(UTC)

    initialize_database(database_path)

    register_funds(
        database_path,
        canonical_funds,
    )

    synchronize_output_file(
        funds=canonical_funds,
        output_path=output_path,
    )

    summary = TwoPassSummary(
        started_at=started_at,
        finished_at=started_at,
        output_path=output_path,
        canonical_funds=len(canonical_funds),
    )

    output_lock = asyncio.Lock()

    summary.fast_results = await _run_pass(
        database_path=database_path,
        output_path=output_path,
        parsed_directory=parsed_directory,
        funds=selected_funds,
        fetcher=fetcher,
        budget=fast_budget,
        crawl_pass=CrawlPass.FAST,
        wanted_by_fund={},
        concurrency=concurrency,
        force=force,
        avant_fallback=avant_fallback,
        amista_fallback=amista_fallback,
        porovnejfondy_fallback=porovnejfondy_fallback,
        anydoc_fallback=anydoc_fallback,
        output_lock=output_lock,
        run_id=f"{run_id}-fast",
        progress=progress,
    )

    crawled_ids = {stable_fund_id(fund) for fund in selected_funds}

    before = field_states(load_output(output_path))

    # What the first pass learned about each fund's official sources is
    # what tells an absent field apart from an unreached document.
    outcomes = {
        result.fund_id: DiscoveryOutcome(
            sufficient=result.official_sources_sufficient,
            exhausted=result.official_stage_exhausted,
            missing_document_groups=(result.official_missing_document_groups),
        )
        for result in summary.fast_results
    }

    plans = [
        plan
        for plan in select_deep_pass_funds(
            output_records=_records_as_payload(output_path),
            audit_findings=audit_findings,
            conflict_records=conflict_records,
            discovery_outcomes=outcomes,
        )
        if plan.fund_id in crawled_ids
    ]

    if deep_limit > 0:
        # The plans are ordered by priority, so a cap keeps the funds
        # whose missing fields matter most.
        plans = plans[:deep_limit]

    summary.plans = plans

    funds_by_id = {stable_fund_id(fund): fund for fund in selected_funds}

    deep_funds = [funds_by_id[plan.fund_id] for plan in plans if plan.fund_id in funds_by_id]

    if deep_funds and not skip_deep_pass:
        summary.deep_results = await _run_pass(
            database_path=database_path,
            output_path=output_path,
            parsed_directory=parsed_directory,
            funds=deep_funds,
            fetcher=fetcher,
            budget=deep_budget,
            crawl_pass=CrawlPass.DEEP,
            wanted_by_fund={plan.fund_id: plan.wanted_document_types for plan in plans},
            concurrency=concurrency,
            force=force,
            avant_fallback=avant_fallback,
            amista_fallback=amista_fallback,
            porovnejfondy_fallback=porovnejfondy_fallback,
            anydoc_fallback=anydoc_fallback,
            output_lock=output_lock,
            run_id=f"{run_id}-deep",
            progress=progress,
        )

        after = field_states(load_output(output_path))

        summary.recovered_fields = _recovered(
            before=before,
            after=after,
            plans=plans,
        )

    summary.finished_at = datetime.now(UTC)

    return summary


async def _run_pass(
    *,
    database_path: Path,
    output_path: Path,
    parsed_directory: Path,
    funds: list[FundInput],
    fetcher: HttpFetcher,
    budget: CrawlBudget,
    crawl_pass: CrawlPass,
    wanted_by_fund: dict[str, frozenset[Any]],
    concurrency: int,
    force: bool,
    avant_fallback: bool,
    amista_fallback: bool,
    porovnejfondy_fallback: bool,
    anydoc_fallback: bool,
    output_lock: asyncio.Lock,
    run_id: str,
    progress: TwoPassProgress | None = None,
) -> list[FundPipelineResult]:
    """Run one pass over a list of funds, at the given budget."""

    if not funds:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))

    results: list[FundPipelineResult | None] = [None] * len(funds)

    completed = 0

    progress_lock = asyncio.Lock()

    async def run_one(
        index: int,
        fund: FundInput,
    ) -> None:
        async with semaphore:
            results[index] = await run_fund_pipeline(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=parsed_directory,
                fund=fund,
                fetcher=fetcher,
                force=force,
                avant_fallback=avant_fallback,
                amista_fallback=amista_fallback,
                porovnejfondy_fallback=porovnejfondy_fallback,
                anydoc_fallback=anydoc_fallback,
                output_lock=output_lock,
                discovery_run_id=run_id,
                budget=budget,
                crawl_pass=crawl_pass,
                wanted_document_types=wanted_by_fund.get(
                    stable_fund_id(fund),
                    frozenset(),
                ),
            )

        if progress is None:
            return

        nonlocal completed

        async with progress_lock:
            completed += 1

            finished = results[index]

            if finished is not None:
                progress(
                    crawl_pass,
                    completed,
                    len(funds),
                    finished,
                )

    await asyncio.gather(*(run_one(index, fund) for index, fund in enumerate(funds)))

    return [item for item in results if item is not None]


def _records_as_payload(
    output_path: Path,
) -> list[dict[str, Any]]:
    """Return the delivered output in the plain shape the planner reads."""

    return [record.model_dump(mode="json") for record in load_output(output_path)]


def _recovered(
    *,
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    plans: Sequence[DeepPassPlan],
) -> list[tuple[str, str, str, str]]:
    """Return every field the deep pass turned into an answer."""

    recovered: list[tuple[str, str, str, str]] = []

    for plan in plans:
        earlier = before.get(plan.fund_id, {})

        later = after.get(plan.fund_id, {})

        for field_name in plan.fields:
            was = earlier.get(field_name, "")

            now = later.get(field_name, "")

            if now == "found" and was != "found":
                recovered.append(
                    (
                        plan.fund_id,
                        plan.fund_name,
                        field_name,
                        was or "unknown",
                    )
                )

    return recovered


def write_two_pass_report(
    *,
    summary: TwoPassSummary,
    report_path: Path,
    cache_hits: int = 0,
    cache_misses: int = 0,
) -> None:
    """Write the report of a two-pass run."""

    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat(),
        "duration_seconds": round(
            (summary.finished_at - summary.started_at).total_seconds(),
            3,
        ),
        "output_path": str(summary.output_path),
        "canonical_funds": summary.canonical_funds,
        "http_cache": {
            "hits": cache_hits,
            "misses": cache_misses,
        },
        "fast_pass": _pass_payload(
            metrics=summary.fast_metrics,
            results=summary.fast_results,
        ),
        "deep_pass": _pass_payload(
            metrics=summary.deep_metrics,
            results=summary.deep_results,
        ),
        "deep_pass_selection": [
            {
                "fund_id": plan.fund_id,
                "fund_name": plan.fund_name,
                "priority": plan.priority,
                "fields": list(plan.fields),
                "wanted_document_types": sorted(item.value for item in plan.wanted_document_types),
                "reasons": [
                    {
                        "field": reason.field,
                        "trigger": reason.trigger.value,
                        "detail": reason.detail,
                    }
                    for reason in plan.reasons
                ],
            }
            for plan in summary.plans
        ],
        "recovered_fields": [
            {
                "fund_id": fund_id,
                "fund_name": fund_name,
                "field": field_name,
                "previous_status": previous,
            }
            for fund_id, fund_name, field_name, previous in summary.recovered_fields
        ],
        "new_documents_in_deep_pass": sum(
            item.documents_downloaded for item in summary.deep_results
        ),
        "failures": [
            {
                "fund_name": item.fund_name,
                "crawl_pass": item.crawl_pass,
                "failures": list(item.failures[:10]),
            }
            for item in (summary.fast_results + summary.deep_results)
            if item.failures
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


def _pass_payload(
    *,
    metrics: PassMetrics,
    results: Sequence[FundPipelineResult],
) -> dict[str, Any]:
    return {
        "funds": metrics.funds,
        "pages_visited": metrics.pages_visited,
        "documents_discovered": metrics.documents_discovered,
        "documents_downloaded": metrics.documents_downloaded,
        "documents_parsed": metrics.documents_parsed,
        "superseded_documents_skipped": metrics.superseded_documents,
        "failures": metrics.failures,
        "expected_misses": metrics.expected_misses,
        "warnings": metrics.warnings,
        "real_failures": metrics.real_failures,
        "fund_seconds": metrics.duration_seconds,
        "stage_seconds": {
            "discovery": metrics.discovery_seconds,
            "crawl": metrics.crawl_seconds,
            "parse": metrics.parse_seconds,
            "extract": metrics.extract_seconds,
        },
        "slowest_funds": [
            {
                "fund_name": item.fund_name,
                "duration_seconds": item.duration_seconds,
                "stage_seconds": item.timings.as_dict(),
                "documents_parsed": item.documents_parsed,
            }
            for item in sorted(
                results,
                key=lambda item: -item.duration_seconds,
            )[:10]
        ],
        "funds_detail": [
            {
                "fund_name": item.fund_name,
                "status": item.status.value,
                "pages_visited": item.pages_visited,
                "documents_discovered": item.documents_discovered,
                "documents_downloaded": item.documents_downloaded,
                "official_sources_sufficient": (item.official_sources_sufficient),
                "official_missing_document_groups": list(item.official_missing_document_groups),
                "fields_found": item.fields_found,
                "duration_seconds": item.duration_seconds,
                "stage_seconds": item.timings.as_dict(),
                "diagnostics": item.diagnostic_counts,
            }
            for item in results
        ],
    }
