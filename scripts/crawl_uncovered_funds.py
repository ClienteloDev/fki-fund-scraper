"""
Crawl only the funds the existing cache does not already cover.

Discovery and downloading run; parsing and extraction do not. The HTTP
cache is shared with the earlier runs, so a page that was already
fetched is reused rather than requested again.

A fund with a known website is explored through the official-source
stage first and only falls back to an adapter when that came up short. A
fund without a known website has nothing official to explore, so the
manager adapters are its only route.

Usage:

    uv run python scripts/crawl_uncovered_funds.py \\
        --input data/input/funds.to-crawl.json \\
        --database cache/regen.sqlite3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fundscraper.config import ConfigurationError, HttpSettings
from fundscraper.crawl_service import crawl_fund_site
from fundscraper.database import (
    DatabaseError,
    initialize_database,
    register_funds,
)
from fundscraper.domain_adapters.base import DomainAdapterResult
from fundscraper.domain_adapters.registry import (
    get_amista_fallback_adapter,
    get_avant_fallback_adapter,
    get_domain_adapter,
    get_porovnejfondy_fallback_adapter,
)
from fundscraper.html_discovery import DiscoveredLink
from fundscraper.http_client import HttpFetcher
from fundscraper.input_loader import InputFileError, load_funds
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_url
from fundscraper.official_discovery import discover_official_sources


@dataclass(frozen=True, slots=True)
class FundCrawlOutcome:
    fund_name: str
    has_website: bool
    official_documents: int
    official_sufficient: bool
    adapter_names: tuple[str, ...]
    pages_visited: int
    documents_discovered: int
    documents_downloaded: int
    failures: int


async def adapter_sources(
    *,
    fund: FundInput,
    fetcher: HttpFetcher,
    force: bool,
) -> DomainAdapterResult:
    """Run every adapter that can reach this fund and merge the result."""

    adapters = []

    explicit = get_domain_adapter(fund)

    if explicit is not None:
        adapters.append(explicit)

    for adapter in (
        get_amista_fallback_adapter(),
        get_avant_fallback_adapter(),
        get_porovnejfondy_fallback_adapter(),
    ):
        if all(existing.name != adapter.name for existing in adapters):
            adapters.append(adapter)

    navigation: dict[str, str] = {}

    documents: dict[str, DiscoveredLink] = {}

    warnings: list[str] = []

    names: list[str] = []

    for adapter in adapters:
        result = await adapter.discover(
            fund=fund,
            fetcher=fetcher,
            force=force,
        )

        names.append(result.adapter_name)

        warnings.extend(result.warnings)

        for url in result.navigation_urls:
            navigation[canonical_url(url)] = url

        for document in result.documents:
            key = canonical_url(document.url)

            existing = documents.get(key)

            if existing is None or document.score > existing.score:
                documents[key] = document

    return DomainAdapterResult(
        adapter_name="+".join(names) if names else "none",
        navigation_urls=tuple(sorted(navigation.values())),
        documents=tuple(documents.values()),
        warnings=tuple(warnings),
    )


async def crawl_one(
    *,
    fund: FundInput,
    database_path: Path,
    fetcher: HttpFetcher,
    max_pages: int,
    max_documents: int,
    force: bool,
) -> FundCrawlOutcome:
    official_documents = 0

    official_sufficient = False

    navigation: tuple[str, ...] = ()

    documents: tuple[DiscoveredLink, ...] = ()

    adapter_names: tuple[str, ...] = ()

    if fund.has_website:
        official = await discover_official_sources(
            database_path=database_path,
            fund=fund,
            fetcher=fetcher,
            force=force,
            run_id="dataset-refresh",
        )

        official_documents = len(official.documents)

        official_sufficient = official.is_sufficient

        navigation = official.navigation_urls

        documents = official.documents

    if not official_sufficient:
        # Either the official site fell short or there is no official
        # site at all. Both are what the adapters exist for.
        adapters = await adapter_sources(
            fund=fund,
            fetcher=fetcher,
            force=force,
        )

        adapter_names = (adapters.adapter_name,)

        navigation = navigation + adapters.navigation_urls

        documents = documents + adapters.documents

    summary = await crawl_fund_site(
        database_path=database_path,
        fund=fund,
        fetcher=fetcher,
        max_pages=max_pages,
        max_documents=max_documents,
        navigation_seed_urls=navigation,
        document_seed_links=documents,
        force=force,
    )

    return FundCrawlOutcome(
        fund_name=fund.name,
        has_website=fund.has_website,
        official_documents=official_documents,
        official_sufficient=official_sufficient,
        adapter_names=adapter_names,
        pages_visited=summary.pages_visited,
        documents_discovered=summary.documents_discovered,
        documents_downloaded=summary.documents_downloaded,
        failures=len(summary.failures),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl_uncovered_funds",
        description="Crawl the funds the existing cache does not cover.",
    )

    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--canonical",
        type=Path,
        default=Path("data/input/funds.json"),
        help=(
            "The full dataset to register. register_funds deletes every "
            "fund absent from what it is given, so it must always be "
            "handed the whole dataset even when only a subset is crawled."
        ),
    )
    parser.add_argument("--database", type=Path, default=Path("cache/regen.sqlite3"))
    parser.add_argument("--cache-directory", type=Path, default=Path("cache/http"))
    parser.add_argument("--report", type=Path, default=Path("reports/dataset-refresh-crawl.json"))
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-documents", type=int, default=25)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")

    return parser


async def run(
    *,
    funds: list[FundInput],
    args: argparse.Namespace,
) -> list[FundCrawlOutcome]:
    settings = HttpSettings.from_environment()

    outcomes: list[FundCrawlOutcome] = []

    async with HttpFetcher(
        settings,
        args.cache_directory.resolve(),
    ) as fetcher:
        for index, fund in enumerate(
            funds,
            start=1,
        ):
            print(
                f"  [{index}/{len(funds)}] {fund.name[:58]}",
                flush=True,
            )

            try:
                outcomes.append(
                    await crawl_one(
                        fund=fund,
                        database_path=args.database.resolve(),
                        fetcher=fetcher,
                        max_pages=args.max_pages,
                        max_documents=args.max_documents,
                        force=args.force,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one fund must never stop the run
                print(
                    f"     failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    return outcomes


def main() -> int:
    args = build_parser().parse_args()

    try:
        funds = load_funds(args.input.resolve())
    except InputFileError as exc:
        print(f"Input could not be loaded: {exc}", file=sys.stderr)

        return 1

    if args.limit:
        funds = funds[: args.limit]

    if not funds:
        print("Nothing to crawl.")

        return 0

    database_path = args.database.resolve()

    try:
        initialize_database(database_path)

        # Registering only the subset would delete every other fund of the
        # database together with its sources, so the whole canonical
        # dataset is registered and only the subset is then crawled.
        canonical_path = args.canonical.resolve()

        register_funds(
            database_path,
            load_funds(canonical_path) if canonical_path.exists() else funds,
        )

        outcomes = asyncio.run(
            run(
                funds=funds,
                args=args,
            )
        )
    except (DatabaseError, ConfigurationError) as exc:
        print(f"Crawl failed: {exc}", file=sys.stderr)

        return 1

    downloaded = sum(item.documents_downloaded for item in outcomes)

    discovered = sum(item.documents_discovered for item in outcomes)

    pages = sum(item.pages_visited for item in outcomes)

    with_documents = sum(1 for item in outcomes if item.documents_downloaded)

    print()
    print(f"Funds crawled          : {len(outcomes)}")
    print(f"  with a website       : {sum(1 for item in outcomes if item.has_website)}")
    print(f"  without a website    : {sum(1 for item in outcomes if not item.has_website)}")
    print(f"Pages visited          : {pages}")
    print(f"Documents discovered   : {discovered}")
    print(f"Documents downloaded   : {downloaded}")
    print(f"Funds with a document  : {with_documents}")
    print(f"Funds with none        : {len(outcomes) - with_documents}")

    adapters: Counter[str] = Counter()

    for item in outcomes:
        adapters.update(item.adapter_names)

    if adapters:
        print()
        print("Adapters used:")

        for name, count in adapters.most_common(10):
            print(f"  {name[:56]:<58}{count}")

    report_path = args.report.resolve()

    report_path.parent.mkdir(parents=True, exist_ok=True)

    payload: list[dict[str, Any]] = [
        {
            "fund": item.fund_name,
            "has_website": item.has_website,
            "official_documents": item.official_documents,
            "official_sufficient": item.official_sufficient,
            "adapters": list(item.adapter_names),
            "pages_visited": item.pages_visited,
            "documents_discovered": item.documents_discovered,
            "documents_downloaded": item.documents_downloaded,
            "failures": item.failures,
        }
        for item in outcomes
    ]

    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print(f"Full report: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
