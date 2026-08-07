"""
Find official websites for funds whose domain is missing in funds.json.

The script is independent of the main crawling pipeline. For every fund
with a null or empty ``web`` value it searches the web for the fund name,
opens only the main page of each candidate domain, and accepts a domain
when the page clearly identifies the requested fund. Fund documents such
as a KID or statute raise confidence but are not required. Generated
domain variations are used only as a fallback when search finds too few
candidates. Unresolved funds are written to ``missing_urls.json``.

Search quality depends on the configured search backend. When
``GOOGLE_SEARCH_API_KEY`` and ``GOOGLE_SEARCH_ENGINE_ID`` are present in
the environment or in ``.env``, a real search API is used. Otherwise the
script falls back to keyless HTML search engines, which apply anti-bot
protection and frequently answer with a protection page instead of
results, leaving more funds for manual review.

Usage examples:

    uv run python scripts/find_missing_domains.py
    uv run python scripts/find_missing_domains.py --limit 10 --dry-run
    uv run python scripts/find_missing_domains.py \\
        --input data/input/funds.json \\
        --missing-output data/output/missing_urls.json \\
        --concurrency 4 \\
        --requests-per-second 1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from fundscraper.config import ConfigurationError, HttpSettings
from fundscraper.domain_backfill import (
    MISSING_URLS_FILE_NAME,
    DomainBackfillError,
    DomainResolutionStatus,
    FundDomainResolution,
    backup_input_file,
    build_missing_domain_report,
    load_fund_entries,
    resolve_missing_domains,
    select_funds_with_missing_domain,
    update_fund_domains,
    write_missing_domain_report,
)
from fundscraper.domain_candidates import (
    BROWSER_USER_AGENT,
    CandidateSource,
    default_candidate_sources,
    default_fallback_sources,
)
from fundscraper.domain_manager_fallback import ManagerAdapterFallback
from fundscraper.domain_verification import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_MINIMUM_SCORE,
    DEFAULT_REVIEW_SCORE,
)
from fundscraper.http_client import HttpFetcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find_missing_domains",
        description=(
            "Discover and verify official fund websites for funds with a "
            "missing domain, and update funds.json only for confidently "
            "verified results."
        ),
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/funds.json"),
        help="Fund list to read and update.",
    )
    parser.add_argument(
        "--missing-output",
        type=Path,
        default=Path("data/output") / MISSING_URLS_FILE_NAME,
        help="Report of funds whose domain could not be verified.",
    )
    parser.add_argument(
        "--cache-directory",
        type=Path,
        default=Path("cache/http"),
        help="Shared HTTP cache directory.",
    )
    parser.add_argument(
        "--backup-directory",
        type=Path,
        default=Path("backups"),
        help="Directory receiving a timestamped copy of the input file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many funds with a missing domain (0 = all).",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many funds with a missing domain.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of funds processed concurrently.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=8,
        help="Maximum candidate domains verified per fund.",
    )
    parser.add_argument(
        "--search-results",
        type=int,
        default=8,
        help="Maximum search result domains per fund (0 disables search).",
    )
    parser.add_argument(
        "--search-queries",
        type=int,
        default=3,
        help=(
            "Search queries per fund: exact legal name, name without legal "
            "suffixes, then variants with fond, SICAV and KID statut."
        ),
    )
    parser.add_argument(
        "--heuristic-results",
        type=int,
        default=4,
        help=(
            "Maximum generated name-based fallback domains per fund, used "
            "only when search returns too few candidates (0 disables them)."
        ),
    )
    parser.add_argument(
        "--no-adapter-fallback",
        action="store_true",
        help=(
            "Do not look the fund up in the catalogs of known managers "
            "and administrators when no standalone website is verified."
        ),
    )
    parser.add_argument(
        "--user-agent",
        default=BROWSER_USER_AGENT,
        help=(
            "HTTP user agent. The default imitates a browser because search "
            "engines and many fund websites reject crawler user agents."
        ),
    )
    parser.add_argument(
        "--minimum-score",
        type=int,
        default=DEFAULT_MINIMUM_SCORE,
        help="Verification score required to accept a domain.",
    )
    parser.add_argument(
        "--review-score",
        type=int,
        default=DEFAULT_REVIEW_SCORE,
        help="Score above which a rejected candidate is reported for review.",
    )
    parser.add_argument(
        "--ambiguity-margin",
        type=int,
        default=DEFAULT_AMBIGUITY_MARGIN,
        help="Score distance required between two accepted candidates.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="HTTP retry attempts for retryable failures.",
    )
    parser.add_argument(
        "--requests-per-second",
        type=float,
        default=2.0,
        help="Global HTTP rate limit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the HTTP cache and download every page again.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report results without modifying the input file.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not copy the input file before updating it.",
    )

    return parser


def report_progress(
    completed: int,
    total: int,
    resolution: FundDomainResolution,
) -> None:
    if resolution.status is DomainResolutionStatus.VERIFIED:
        detail = str(resolution.resolved_url)
    else:
        detail = resolution.reason

    print(
        f"[{completed}/{total}] {resolution.status.value.upper():<16} "
        f"{resolution.entry.name} -> {detail}",
        flush=True,
    )


async def run(
    args: argparse.Namespace,
) -> int:
    input_path = args.input.resolve()

    entries = load_fund_entries(input_path)

    missing_entries = select_funds_with_missing_domain(entries)

    if args.offset < 0:
        raise ValueError("offset must not be negative")

    if args.limit < 0:
        raise ValueError("limit must not be negative")

    selected_entries = missing_entries[args.offset :]

    if args.limit:
        selected_entries = selected_entries[: args.limit]

    print(f"Input file: {input_path}")
    print(f"Funds total: {len(entries)}")
    print(f"Funds with a missing domain: {len(missing_entries)}")
    print(f"Funds selected for this run: {len(selected_entries)}")
    print()

    if not selected_entries:
        print("Nothing to do.")

        return 0

    settings = HttpSettings(
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
        requests_per_second=(args.requests_per_second),
        user_agent=args.user_agent,
    )

    sources = default_candidate_sources(
        search_results=args.search_results,
        search_queries=args.search_queries,
    )

    fallback_sources = default_fallback_sources(
        heuristic_results=(args.heuristic_results),
    )

    adapter_sources: tuple[CandidateSource, ...] = (
        () if args.no_adapter_fallback else (ManagerAdapterFallback(),)
    )

    if not sources and not fallback_sources and not adapter_sources:
        raise ValueError("At least one candidate source must remain enabled")

    async with HttpFetcher(
        settings,
        args.cache_directory.resolve(),
    ) as fetcher:
        resolutions = await resolve_missing_domains(
            entries=selected_entries,
            fetcher=fetcher,
            sources=sources,
            fallback_sources=fallback_sources,
            adapter_sources=adapter_sources,
            concurrency=args.concurrency,
            max_candidates=args.max_candidates,
            minimum_score=args.minimum_score,
            review_score=args.review_score,
            ambiguity_margin=args.ambiguity_margin,
            force=args.force,
            progress_callback=report_progress,
        )

    verified_count = sum(
        1 for resolution in resolutions if resolution.status is DomainResolutionStatus.VERIFIED
    )

    domains_added = 0

    if verified_count and not args.dry_run and not args.no_backup:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

        backup_path = backup_input_file(
            path=input_path,
            directory=(args.backup_directory.resolve() / f"find-missing-domains-{timestamp}"),
        )

        if backup_path is not None:
            print()
            print(f"Input backup: {backup_path}")

    domains_added = update_fund_domains(
        path=input_path,
        resolutions=resolutions,
        dry_run=args.dry_run,
    )

    report = build_missing_domain_report(
        input_path=input_path,
        funds_total=len(entries),
        resolutions=resolutions,
        domains_added=domains_added,
    )

    write_missing_domain_report(
        report=report,
        path=args.missing_output.resolve(),
    )

    errors = report.summary.errors

    print()
    print("=" * 72)
    print("MISSING DOMAIN DISCOVERY SUMMARY")
    print("=" * 72)
    print(f"Funds checked:     {report.summary.funds_checked}")
    print(f"Domains added:     {domains_added}{' (dry run)' if args.dry_run else ''}")
    print(f"Unresolved funds:  {report.summary.unresolved}")
    print(f"Errors:            {errors}")
    print(f"Unresolved report: {args.missing_output.resolve()}")
    print()

    return 0


def main() -> int:
    args = build_parser().parse_args()

    try:
        return asyncio.run(run(args))
    except (
        ConfigurationError,
        DomainBackfillError,
        ValueError,
    ) as exc:
        print(
            f"Domain discovery failed: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            "\nRun interrupted by user.",
            file=sys.stderr,
        )

        raise SystemExit(130) from None
