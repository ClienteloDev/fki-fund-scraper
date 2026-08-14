"""
Phase 2 of the 8-fund crawler recovery experiment: the true online run.

Every fund starts from its canonical ``web`` value in ``data/input/funds.json``
and from nothing else. No navigation seed, no document seed and no fallback
adapter is supplied, so whatever the run reaches, the crawler reached on its
own. The deep budget is used because it is the largest the production two-pass
run ever grants a fund: a page the crawler misses under it was missed by
navigation, not by the budget.

Everything is written under ``cache/sample8-crawler-recovery/``; the production
database, HTTP cache and parsed cache are never opened.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from sample8_baseline import SAMPLE_FUND_NAMES  # noqa: E402

from fundscraper.config import HttpSettings  # noqa: E402
from fundscraper.crawl_planning import DEEP_BUDGET, CrawlPass  # noqa: E402
from fundscraper.database import initialize_database, register_funds  # noqa: E402
from fundscraper.field_extraction import build_official_site_index  # noqa: E402
from fundscraper.http_client import HttpFetcher  # noqa: E402
from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.models import FundInput  # noqa: E402
from fundscraper.pipeline_service import (  # noqa: E402
    run_fund_pipeline,
    synchronize_output_file,
)

SAMPLE_ROOT = REPOSITORY_ROOT / "cache/sample8-crawler-recovery"


def selected_funds(funds: list[FundInput]) -> list[FundInput]:
    by_name = {fund.name: fund for fund in funds}

    return [by_name[name] for name in SAMPLE_FUND_NAMES]


async def run(*, generation: str, force: bool) -> dict[str, object]:
    funds = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    sample = selected_funds(funds)

    run_root = SAMPLE_ROOT / generation

    run_root.mkdir(parents=True, exist_ok=True)

    database_path = run_root / "sample8.sqlite3"

    output_path = run_root / "funds.enriched.json"

    initialize_database(database_path)

    # The whole canonical dataset is registered, never the sample alone.
    register_funds(database_path, funds)

    synchronize_output_file(funds=funds, output_path=output_path)

    # Which hosts are one fund's own official website, decided from the
    # canonical input alone.
    official_site = build_official_site_index(funds)

    settings = HttpSettings.from_environment()

    results: list[dict[str, object]] = []

    started_at = datetime.now(UTC)

    async with HttpFetcher(settings, SAMPLE_ROOT / "http") as fetcher:
        for index, fund in enumerate(sample, start=1):
            print(f"[{index}/{len(sample)}] {fund.name} -> {fund.web}", flush=True)

            result = await run_fund_pipeline(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=SAMPLE_ROOT / "parsed",
                fund=fund,
                fetcher=fetcher,
                budget=DEEP_BUDGET,
                crawl_pass=CrawlPass.DEEP,
                official_site=official_site,
                force=force,
                # Explicitly no third-party route: the experiment asks
                # what the fund's own site yields.
                avant_fallback=False,
                amista_fallback=False,
                porovnejfondy_fallback=False,
                anydoc_fallback=False,
            )

            print(
                f"    status={result.status.value} "
                f"pages={result.pages_visited} "
                f"docs={result.documents_downloaded}/{result.documents_discovered} "
                f"parsed={result.documents_parsed} "
                f"core={result.fields_found}/5 "
                f"{result.duration_seconds}s",
                flush=True,
            )

            results.append(
                {
                    "fund_id": result.fund_id,
                    "fund_name": result.fund_name,
                    "input_web": fund.web,
                    "status": result.status.value,
                    "pages_visited": result.pages_visited,
                    "official_pages_inspected": result.official_pages_inspected,
                    "official_documents_found": result.official_documents_found,
                    "official_documents_rejected": result.official_documents_rejected,
                    "official_missing_document_groups": list(
                        result.official_missing_document_groups
                    ),
                    "documents_discovered": result.documents_discovered,
                    "documents_downloaded": result.documents_downloaded,
                    "documents_parsed": result.documents_parsed,
                    "core_fields_found": result.fields_found,
                    "field_statuses": dict(result.field_statuses),
                    "extended_statuses": dict(result.extended_statuses),
                    "discovery_method_counts": dict(result.discovery_method_counts),
                    "failures": list(result.failures),
                    "timings": result.timings.as_dict(),
                    "duration_seconds": result.duration_seconds,
                }
            )

    payload = {
        "generation": generation,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "budget": {
            "official_max_pages": DEEP_BUDGET.official_max_pages,
            "official_max_documents": DEEP_BUDGET.official_max_documents,
            "max_pages": DEEP_BUDGET.max_pages,
            "max_depth": DEEP_BUDGET.max_depth,
            "max_documents": DEEP_BUDGET.max_documents,
        },
        "seeds": "canonical FundInput.web only",
        "database": str(database_path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "output": str(output_path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
        "funds": results,
    }

    report_path = REPOSITORY_ROOT / f"reports/sample8-crawl-{generation}.json"

    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nwritten {report_path}")

    return payload


def main() -> int:
    generation = sys.argv[1] if len(sys.argv) > 1 else "before"

    force = "--force" in sys.argv

    asyncio.run(run(generation=generation, force=force))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
