"""
What the two-pass run does, and what its options really mean.

The cases here guard behaviour that a documentation review found stated
one way and implemented another: a dry run of the selection has to stay a
dry run, and limiting a run to a sample must never shrink the dataset to
the sample.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from fundscraper.config import HttpSettings
from fundscraper.crawl_planning import CrawlPass
from fundscraper.database import get_database_status
from fundscraper.http_client import HttpFetcher
from fundscraper.models import FundInput
from fundscraper.output_service import load_output, stable_fund_id
from fundscraper.two_pass_service import (
    TwoPassSummary,
    run_two_pass,
    write_two_pass_report,
)

# The page answers the horizon and nothing else, so every other trigger
# field stays unanswered and the fund is always selected for a deep pass.
PAGE = b"""
<!doctype html>
<html>
  <head><title>Example Fund</title></head>
  <body>
    <p>Doporuceny investicni horizont je 5 let.</p>
  </body>
</html>
"""


CANONICAL = [
    FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/",
    ),
    FundInput(
        name="Second SICAV a.s.",
        web="https://second.example.com/",
    ),
    FundInput(
        name="Third SICAV a.s.",
        web="https://third.example.com/",
    ),
]


def run(
    *,
    tmp_path: Path,
    selected: list[FundInput],
    skip_deep_pass: bool,
    deep_limit: int = 0,
    progress: Any = None,
) -> tuple[TwoPassSummary, Path, Path]:
    database_path = tmp_path / "two-pass.sqlite3"

    output_path = tmp_path / "funds.full.json"

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/html"},
            content=PAGE,
            request=request,
        )

    async def go() -> TwoPassSummary:
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
            return await run_two_pass(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=(tmp_path / "parsed"),
                canonical_funds=CANONICAL,
                selected_funds=selected,
                fetcher=fetcher,
                concurrency=1,
                skip_deep_pass=skip_deep_pass,
                deep_limit=deep_limit,
                progress=progress,
            )

    return (
        asyncio.run(go()),
        database_path,
        output_path,
    )


def test_skip_deep_pass_selects_without_crawling(
    tmp_path: Path,
) -> None:
    """
    The dry run reports what a deep pass would do and does none of it.

    Before this was fixed the option only lowered the deep budget, so a
    reader of the help text paid for a second crawl of every selected
    fund.
    """

    summary, _, _ = run(
        tmp_path=tmp_path,
        selected=[CANONICAL[0]],
        skip_deep_pass=True,
    )

    # The selection is still computed and reported.
    assert summary.plans

    assert summary.plans[0].fund_id == stable_fund_id(CANONICAL[0])

    assert summary.plans[0].wanted_document_types

    # But nothing was crawled a second time.
    assert summary.deep_results == []

    assert summary.deep_metrics.funds == 0

    assert summary.deep_metrics.pages_visited == 0

    assert summary.recovered_fields == []


def test_the_deep_pass_runs_when_it_is_not_skipped(
    tmp_path: Path,
) -> None:
    summary, _, _ = run(
        tmp_path=tmp_path,
        selected=[CANONICAL[0]],
        skip_deep_pass=False,
    )

    assert summary.plans

    assert summary.deep_metrics.funds == 1

    assert all(item.crawl_pass == CrawlPass.DEEP.value for item in summary.deep_results)


def test_a_sample_run_still_registers_the_whole_canonical_dataset(
    tmp_path: Path,
) -> None:
    """Limiting what is crawled must never shrink the dataset to it."""

    summary, database_path, output_path = run(
        tmp_path=tmp_path,
        selected=[CANONICAL[0]],
        skip_deep_pass=True,
    )

    assert summary.canonical_funds == len(CANONICAL)

    assert get_database_status(database_path).funds_total == len(CANONICAL)

    assert len(load_output(output_path)) == len(CANONICAL)

    # Only the selected fund was crawled.
    assert summary.fast_metrics.funds == 1


def test_the_report_records_both_passes_and_the_selection(
    tmp_path: Path,
) -> None:
    summary, _, _ = run(
        tmp_path=tmp_path,
        selected=[CANONICAL[0]],
        skip_deep_pass=True,
    )

    report_path = tmp_path / "step7-two-pass.json"

    write_two_pass_report(
        summary=summary,
        report_path=report_path,
        cache_hits=7,
        cache_misses=3,
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["canonical_funds"] == len(CANONICAL)

    assert payload["http_cache"] == {"hits": 7, "misses": 3}

    assert payload["fast_pass"]["funds"] == 1

    assert payload["deep_pass"]["funds"] == 0

    assert payload["deep_pass_selection"]

    assert payload["deep_pass_selection"][0]["fields"]


def test_the_deep_pass_can_be_capped_to_the_most_valuable_funds(
    tmp_path: Path,
) -> None:
    """
    A run that selects almost every fund still has to be affordable.

    The plans are ordered by priority, so a cap keeps the funds whose
    missing fields matter most rather than an arbitrary prefix.
    """

    summary, _, _ = run(
        tmp_path=tmp_path,
        selected=CANONICAL,
        skip_deep_pass=False,
        deep_limit=2,
    )

    assert len(summary.plans) == 2

    assert summary.deep_metrics.funds == 2

    # Ordered strongest first.
    assert summary.plans[0].priority >= summary.plans[1].priority


def test_progress_is_reported_for_both_passes(
    tmp_path: Path,
) -> None:
    seen: list[tuple[str, int, int]] = []

    run(
        tmp_path=tmp_path,
        selected=[CANONICAL[0]],
        skip_deep_pass=False,
        progress=lambda crawl_pass, done, total, result: seen.append(
            (crawl_pass.value, done, total)
        ),
    )

    assert ("fast", 1, 1) in seen

    assert ("deep", 1, 1) in seen


def test_the_stage_timings_are_recorded(
    tmp_path: Path,
) -> None:
    """Eleven hours of runtime is only explainable if each stage is timed."""

    summary, _, _ = run(
        tmp_path=tmp_path,
        selected=[CANONICAL[0]],
        skip_deep_pass=True,
    )

    timings = summary.fast_results[0].timings

    assert timings.total > 0

    assert timings.as_dict().keys() == {
        "discovery",
        "crawl",
        "parse",
        "extract",
        "measured_total",
    }

    # The measured stages account for essentially all of the fund's wall
    # clock, so a slow fund can be attributed to a stage.
    assert timings.total <= summary.fast_results[0].duration_seconds + 0.5
