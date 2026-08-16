"""
Crawl one validation batch, starting only from each canonical FundInput.web.

No navigation seed, no document seed and no fallback adapter is supplied, so
whatever the run reaches, the crawler reached on its own. The deep budget is
used because it is the largest the production two-pass run ever grants a fund:
a page missed under it was missed by navigation, not by the budget.

``--src`` points the import at a different source tree, which is how the
before-fixes generation is produced: the same script, the same start pages and
the same HTTP cache, against the pre-fix commit checked out in a git worktree.
Everything is written under the batch's own cache directory; the production
database, HTTP cache and parsed cache are never opened.

Usage::

    uv run python scripts/batch_crawl.py <batch> <generation> [--src PATH] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="batch_crawl")

    parser.add_argument("batch")

    parser.add_argument("generation")

    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="Source tree to import fundscraper from. Defaults to this repository.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the HTTP and parsed caches.",
    )

    return parser.parse_args()


ARGUMENTS = parse_arguments()

sys.path.insert(0, str(SCRIPT_DIRECTORY))

# The source tree is chosen before fundscraper is imported, so that one script
# can run both generations.
sys.path.insert(0, str(ARGUMENTS.src or (REPOSITORY_ROOT / "src")))

from batch_selection import resolve  # noqa: E402

from fundscraper.config import HttpSettings  # noqa: E402
from fundscraper.crawl_planning import DEEP_BUDGET, CrawlPass  # noqa: E402
from fundscraper.database import initialize_database, register_funds  # noqa: E402
from fundscraper.http_client import HttpFetcher  # noqa: E402
from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.models import FundInput  # noqa: E402
from fundscraper.pipeline_service import (  # noqa: E402
    run_fund_pipeline,
    synchronize_output_file,
)


def official_site_register(funds: list[FundInput]) -> dict[str, str] | None:
    """
    Return the official-website register, when this source tree has one.

    The register was withdrawn after the batch10 validation showed it
    adopting a manager's material for a fund; a tree that still carries it
    is measured with it, and one that does not is measured without.
    """

    if "official_site" not in inspect.signature(run_fund_pipeline).parameters:
        return None

    from fundscraper.field_extraction import build_official_site_index  # type: ignore[attr-defined]

    return build_official_site_index(funds)


async def run(*, batch_name: str, generation: str, force: bool) -> dict[str, object]:
    batch = resolve(batch_name)

    funds = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    by_name = {fund.name: fund for fund in funds}

    missing = [name for name in batch.fund_names if name not in by_name]

    if missing:
        raise SystemExit(f"batch {batch.name} names funds absent from the input: {missing}")

    selected = [by_name[name] for name in batch.fund_names]

    run_root = batch.root / generation

    run_root.mkdir(parents=True, exist_ok=True)

    database_path = run_root / "batch.sqlite3"

    output_path = run_root / "funds.enriched.json"

    initialize_database(database_path)

    # The whole canonical dataset is registered, never the batch alone.
    register_funds(database_path, funds)

    synchronize_output_file(funds=funds, output_path=output_path)

    official_site = official_site_register(funds)

    print(
        f"batch={batch.name} generation={generation} funds={len(selected)} "
        f"site_register={'yes' if official_site else 'no'} "
        f"src={ARGUMENTS.src or 'repository'}",
        flush=True,
    )

    settings = HttpSettings.from_environment()

    extra: dict[str, object] = {} if official_site is None else {"official_site": official_site}

    results: list[dict[str, object]] = []

    started_at = datetime.now(UTC)

    async with HttpFetcher(settings, batch.root / "http") as fetcher:
        for index, fund in enumerate(selected, start=1):
            print(f"[{index}/{len(selected)}] {fund.name} -> {fund.web}", flush=True)

            result = await run_fund_pipeline(
                database_path=database_path,
                output_path=output_path,
                parsed_directory=batch.root / "parsed",
                fund=fund,
                fetcher=fetcher,
                budget=DEEP_BUDGET,
                crawl_pass=CrawlPass.DEEP,
                force=force,
                avant_fallback=False,
                amista_fallback=False,
                porovnejfondy_fallback=False,
                anydoc_fallback=False,
                **extra,  # type: ignore[arg-type]
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
                    "role": batch.role_of(fund.name),
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
        "batch": batch.name,
        "generation": generation,
        "source_tree": str(ARGUMENTS.src) if ARGUMENTS.src else "repository",
        "official_site_register": official_site is not None,
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

    report_path = REPOSITORY_ROOT / f"reports/{batch.name}-crawl-{generation}.json"

    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nwritten {report_path}")

    return payload


def main() -> int:
    asyncio.run(
        run(
            batch_name=ARGUMENTS.batch,
            generation=ARGUMENTS.generation,
            force=ARGUMENTS.force,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
