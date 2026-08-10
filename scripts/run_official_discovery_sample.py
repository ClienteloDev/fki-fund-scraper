"""
Run official-source discovery over a small sample of funds.

The script exercises the official stage on its own, without the fallback
adapters and without downloading or parsing anything. It reports what the
stage reached for each fund and compares it with the documents a previous
run already recorded on the same official domain, which is what shows
whether deeper official discovery actually found more.

Only the funds named in the input file are visited.

Usage:

    uv run python scripts/run_official_discovery_sample.py \\
        --input data/input/step5-discovery-sample.json \\
        --baseline cache/regen.sqlite3 \\
        --database cache/step5-discovery.sqlite3 \\
        --report reports/step5-discovery-sample.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fundscraper.config import ConfigurationError, HttpSettings
from fundscraper.database import initialize_database, register_funds
from fundscraper.html_discovery import is_direct_document_url
from fundscraper.http_client import HttpFetcher
from fundscraper.input_loader import InputFileError, load_funds
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain
from fundscraper.official_discovery import (
    OfficialDiscoveryResult,
    discover_official_sources,
)
from fundscraper.output_service import stable_fund_id


def baseline_counts(
    *,
    baseline_path: Path,
    fund: FundInput,
) -> tuple[int, int]:
    """
    Return the pages and official documents a previous run recorded.

    The comparison is limited to the official domain of the fund, because
    a document a fallback adapter found on a third-party host says
    nothing about how well the official site was explored.
    """

    if not baseline_path.exists():
        return (0, 0)

    official_domain = canonical_domain(fund.web)

    try:
        with sqlite3.connect(baseline_path) as connection:
            rows = connection.execute(
                "SELECT url FROM sources WHERE fund_id = ?",
                (stable_fund_id(fund),),
            ).fetchall()
    except sqlite3.Error:
        return (0, 0)

    pages = 0

    documents = 0

    for (url,) in rows:
        if is_direct_document_url(str(url)):
            if canonical_domain(str(url)) == official_domain:
                documents += 1
        else:
            pages += 1

    return (
        pages,
        documents,
    )


async def discover_all(
    *,
    funds: list[FundInput],
    database_path: Path,
    cache_directory: Path,
    max_pages: int,
    max_documents: int,
    force: bool,
    run_id: str,
) -> list[OfficialDiscoveryResult]:
    settings = HttpSettings.from_environment()

    results: list[OfficialDiscoveryResult] = []

    async with HttpFetcher(
        settings,
        cache_directory,
    ) as fetcher:
        for index, fund in enumerate(
            funds,
            start=1,
        ):
            print(
                f"  [{index}/{len(funds)}] {fund.name}",
                flush=True,
            )

            results.append(
                await discover_official_sources(
                    database_path=database_path,
                    fund=fund,
                    fetcher=fetcher,
                    max_pages=max_pages,
                    max_documents=max_documents,
                    force=force,
                    run_id=run_id,
                )
            )

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_official_discovery_sample",
        description=("Explore the official sources of a small sample of funds and report them."),
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/step5-discovery-sample.json"),
        help="Fund list to explore.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("cache/regen.sqlite3"),
        help="Database of a previous run, used for the before column.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("cache/step5-discovery.sqlite3"),
        help="Database the discovery log is written to.",
    )
    parser.add_argument(
        "--cache-directory",
        type=Path,
        default=Path("cache/http"),
        help="HTTP cache shared with the rest of the pipeline.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/step5-discovery-sample.json"),
        help="Where to write the JSON report.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=18,
        help="How many official pages may be fetched per fund.",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--run-id",
        default="step5-sample",
        help="Label separating this run in the discovery log.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached HTTP responses.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        funds = load_funds(args.input.resolve())
    except InputFileError as exc:
        print(
            f"Input could not be loaded: {exc}",
            file=sys.stderr,
        )

        return 1

    database_path = args.database.resolve()

    try:
        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        results = asyncio.run(
            discover_all(
                funds=funds,
                database_path=database_path,
                cache_directory=args.cache_directory.resolve(),
                max_pages=args.max_pages,
                max_documents=args.max_documents,
                force=args.force,
                run_id=args.run_id,
            )
        )
    except ConfigurationError as exc:
        print(
            f"Discovery failed: {exc}",
            file=sys.stderr,
        )

        return 1

    rows: list[dict[str, Any]] = []

    method_totals: Counter[str] = Counter()

    for fund, result in zip(
        funds,
        results,
        strict=True,
    ):
        before_pages, before_documents = baseline_counts(
            baseline_path=args.baseline.resolve(),
            fund=fund,
        )

        method_totals.update(result.metrics.method_counts)

        rows.append(
            {
                "fund": fund.name,
                "official_url": fund.web,
                "official_domain": result.official_domain,
                "before_pages": before_pages,
                "before_official_documents": before_documents,
                "after_pages_inspected": (result.metrics.pages_inspected),
                "after_pages_fetched": (result.metrics.pages_fetched),
                "after_official_documents": (result.metrics.documents_accepted),
                "documents_rejected": (result.metrics.documents_rejected),
                "sitemaps_read": result.metrics.sitemaps_read,
                "exploration_complete": result.is_exhausted,
                "sources_sufficient": result.is_sufficient,
                "missing_document_groups": list(result.missing_document_groups),
                "method_counts": (result.metrics.method_counts),
                "document_types": dict(
                    sorted(
                        Counter(
                            document.document_type.value for document in result.documents
                        ).items()
                    )
                ),
                "documents": [
                    {
                        "url": document.url,
                        "score": document.score,
                        "document_type": (document.document_type.value),
                        "text": document.text,
                    }
                    for document in result.documents[:40]
                ],
                "warnings": list(result.warnings[:10]),
            }
        )

    print()
    print(
        f"{'fund':<38}{'pages':>7}{'docs before':>13}"
        f"{'docs after':>12}{'sitemaps':>10}{'enough':>8}"
    )
    print("-" * 90)

    for row in rows:
        print(
            f"{str(row['fund'])[:37]:<38}"
            f"{row['after_pages_fetched']:>7}"
            f"{row['before_official_documents']:>13}"
            f"{row['after_official_documents']:>12}"
            f"{row['sitemaps_read']:>10}"
            f"{'yes' if row['sources_sufficient'] else 'no':>8}"
        )

    print()
    print("Discovery methods across the sample:")

    for method, occurrences in sorted(method_totals.items()):
        print(f"  {method:<20}{occurrences}")

    report_path = args.report.resolve()

    report_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_path.write_text(
        json.dumps(
            {
                "funds": rows,
                "method_totals": dict(sorted(method_totals.items())),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(f"Full report: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
