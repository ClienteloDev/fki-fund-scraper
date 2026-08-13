from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Annotated

import typer

from fundscraper.config import (
    ConfigurationError,
    HttpSettings,
)
from fundscraper.crawl_planning import CrawlPass
from fundscraper.crawl_service import (
    CrawlError,
    CrawlSummary,
    crawl_fund_site,
)
from fundscraper.database import (
    DatabaseError,
    FundStatus,
    get_database_status,
    initialize_database,
    register_funds,
    reset_database,
    validate_database,
)
from fundscraper.delivery_export import (
    DeliveryExportError,
    audit_mismatch_reason,
    build_delivery_records,
    delivery_summary,
    input_digest,
    load_audit,
    load_records,
    write_delivery_output,
)
from fundscraper.discovery_service import (
    DiscoverySummary,
    discover_fund_start_page,
)
from fundscraper.document_parser import (
    DocumentParseError,
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
    ExtractionServiceError,
    ExtractionSummary,
    extract_fund_data,
)
from fundscraper.fallback_planner import (
    FallbackPlanError,
    build_fallback_plan,
    write_fallback_plan,
)
from fundscraper.fallback_sources import (
    FallbackSourceConfigError,
    load_approved_fallback_sources,
)
from fundscraper.fetch_service import (
    fetch_fund_start_page,
)
from fundscraper.grounded_application import (
    GroundedApplicationSummary,
    apply_grounded_decisions,
)
from fundscraper.grounded_decisions import (
    GroundedDecisionError,
    load_grounded_decisions,
)
from fundscraper.grounding_packets import (
    GroundingBatchReport,
    GroundingPacketError,
    build_grounding_packets,
    load_grounding_packets,
    write_grounding_packets,
)
from fundscraper.html_discovery import (
    HtmlDiscoveryError,
)
from fundscraper.http_client import (
    FetchError,
    FetchResult,
    HttpFetcher,
)
from fundscraper.input_loader import InputFileError, load_funds
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain, canonical_url
from fundscraper.official_discovery import (
    OfficialDiscoveryResult,
    discover_official_sources,
)
from fundscraper.output_service import (
    OutputFileError,
    RetryMergeSummary,
    create_pending_output,
    load_output,
    merge_improved_outputs,
    stable_fund_id,
    write_output,
    write_output_schema,
)
from fundscraper.pipeline_service import (
    BatchPipelineSummary,
    FundPipelineResult,
    run_fund_batch,
    run_fund_pipeline,
    synchronize_output_file,
    write_batch_report,
)
from fundscraper.two_pass_service import (
    TwoPassSummary,
    run_two_pass,
    write_two_pass_report,
)

app = typer.Typer(
    name="fundscraper",
    help="Enrich Czech qualified investor fund data from public sources.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """FKI fund data enrichment command-line application."""


@app.command("validate-input")
def validate_input(
    input_path: Annotated[
        Path,
        typer.Argument(
            help="Path to the source funds.json file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
) -> None:
    """Validate the source JSON and print basic dataset statistics."""

    try:
        funds = load_funds(input_path)
    except InputFileError as exc:
        typer.echo(
            f"Input validation failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    domains = Counter(canonical_domain(fund.web or "") for fund in funds)

    canonical_urls = [canonical_url(fund.web or "") for fund in funds if fund.web]

    unique_domains = len(domains)
    unique_urls = len(set(canonical_urls))

    duplicate_url_records = len(canonical_urls) - unique_urls

    shared_domains = sum(1 for count in domains.values() if count > 1)

    typer.echo(f"Input file: {input_path}")
    typer.echo(f"Funds: {len(funds)}")
    typer.echo(f"Unique URLs: {unique_urls}")
    typer.echo(f"Duplicate URL records: {duplicate_url_records}")
    typer.echo(f"Unique domains: {unique_domains}")
    typer.echo(f"Shared domains: {shared_domains}")
    typer.echo("Input validation passed.")


@app.command("init-output")
def init_output(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to the source funds.json file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Path for the pending enriched output file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing output file.",
        ),
    ] = False,
) -> None:
    """Create a validated output skeleton for every input fund."""

    try:
        funds = load_funds(input_path)

        output = create_pending_output(funds)

        write_output(
            output_path,
            output,
            overwrite=force,
        )
    except (InputFileError, OutputFileError) as exc:
        typer.echo(
            f"Output initialization failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Funds initialized: {len(output)}")
    typer.echo(f"Output file: {output_path}")


@app.command("validate-output")
def validate_output(
    output_path: Annotated[
        Path,
        typer.Argument(
            help="Path to the enriched output JSON file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/output/funds.enriched.json"),
) -> None:
    """Validate an enriched output file against the data model."""

    try:
        funds = load_output(output_path)
    except OutputFileError as exc:
        typer.echo(
            f"Output validation failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Output file: {output_path}")
    typer.echo(f"Funds: {len(funds)}")
    typer.echo("Output validation passed.")


@app.command("generate-schema")
def generate_schema(
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Path for the generated JSON Schema file.",
            dir_okay=False,
        ),
    ] = Path("schemas/extended-output.schema.json"),
) -> None:
    """Generate JSON Schema for the enriched output data."""

    write_output_schema(output_path)

    typer.echo(f"Schema file: {output_path}")


@app.command("init-db")
def init_db(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to the source funds.json file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="Path to the SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    reset: Annotated[
        bool,
        typer.Option(
            "--reset",
            help="Delete the existing database before initialization.",
        ),
    ] = False,
) -> None:
    """Initialize the processing database and register input funds."""

    try:
        funds = load_funds(input_path)

        if reset:
            reset_database(database_path)

        initialize_database(database_path)

        registered_count = register_funds(
            database_path,
            funds,
        )
    except (InputFileError, DatabaseError) as exc:
        typer.echo(
            f"Database initialization failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Database file: {database_path}")
    typer.echo(f"Funds registered: {registered_count}")
    typer.echo("Database initialization passed.")


@app.command("db-status")
def db_status(
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="Path to the SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
) -> None:
    """Display processing database statistics."""

    try:
        status = get_database_status(database_path)
    except DatabaseError as exc:
        typer.echo(
            f"Database status failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Database file: {database_path}")
    typer.echo(f"Schema version: {status.schema_version}")
    typer.echo(f"Funds total: {status.funds_total}")
    typer.echo(f"Funds pending: {status.funds_pending}")
    typer.echo(f"Funds in progress: {status.funds_in_progress}")
    typer.echo(f"Funds completed: {status.funds_completed}")
    typer.echo(f"Funds partial: {status.funds_partial}")
    typer.echo(f"Funds failed: {status.funds_failed}")
    typer.echo(f"Sources total: {status.sources_total}")
    typer.echo(f"Attempts total: {status.attempts_total}")
    typer.echo(f"Parsed documents total: {status.parsed_documents_total}")


@app.command("validate-db")
def validate_db(
    database_path: Annotated[
        Path,
        typer.Argument(
            help="Path to the SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
) -> None:
    """Validate SQLite integrity and foreign-key relationships."""

    try:
        validate_database(database_path)
    except DatabaseError as exc:
        typer.echo(
            f"Database validation failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Database file: {database_path}")
    typer.echo("Database validation passed.")


@app.command("fetch-start-page")
def fetch_start_page_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact name of the fund from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to the source funds.json file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="Path to the SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="Directory used for downloaded response cache.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore a cached response and download it again.",
        ),
    ] = False,
) -> None:
    """Download and record the original website of one fund."""

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if fund.name.casefold() == fund_name.casefold()]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        if len(matching_funds) > 1:
            typer.echo(
                f"Fund name is not unique: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        settings = HttpSettings.from_environment()

        async def run_fetch() -> FetchResult:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await fetch_fund_start_page(
                    database_path=database_path,
                    fund=fund,
                    fetcher=fetcher,
                    force=force,
                )

        result = asyncio.run(run_fetch())
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        FetchError,
    ) as exc:
        typer.echo(
            f"Start page fetch failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {fund.name}")
    typer.echo(f"Requested URL: {result.requested_url}")
    typer.echo(f"Final URL: {result.final_url}")
    typer.echo(f"HTTP status: {result.status_code}")
    typer.echo(f"Content type: {result.content_type}")
    typer.echo(f"Downloaded bytes: {len(result.body)}")
    typer.echo(f"SHA-256: {result.sha256}")
    typer.echo(f"Cache hit: {result.from_cache}")
    typer.echo(f"Local file: {result.local_path}")


@app.command("discover-start-page")
def discover_start_page_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact name of the fund from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to the source funds.json file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="Path to the SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="Directory used for downloaded response cache.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore cached start page and download it again.",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            max=100,
            help="Maximum number of candidates printed.",
        ),
    ] = 20,
) -> None:
    """Discover relevant document links on one fund start page."""

    summary: DiscoverySummary

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if fund.name.casefold() == fund_name.casefold()]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        if len(matching_funds) > 1:
            typer.echo(
                f"Fund name is not unique: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        settings = HttpSettings.from_environment()

        async def run_discovery() -> DiscoverySummary:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await discover_fund_start_page(
                    database_path=database_path,
                    fund=fund,
                    fetcher=fetcher,
                    force=force,
                )

        summary = asyncio.run(run_discovery())
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        FetchError,
        HtmlDiscoveryError,
    ) as exc:
        typer.echo(
            f"Start page discovery failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {summary.fund_name}")
    typer.echo(f"Page URL: {summary.page_url}")
    typer.echo(f"Page title: {summary.page_title or '-'}")
    typer.echo(f"Links inspected: {summary.links_total}")
    typer.echo(f"Candidates found: {len(summary.candidates)}")

    for index, candidate in enumerate(
        summary.candidates[:limit],
        start=1,
    ):
        typer.echo("")
        typer.echo(f"{index}. [{candidate.score}] {candidate.document_type.value}")
        typer.echo(f"   Text: {candidate.text or '-'}")
        typer.echo(f"   URL: {candidate.url}")
        typer.echo(f"   Direct document: {candidate.direct_document}")
        typer.echo(f"   Same domain: {candidate.same_domain}")


@app.command("crawl-fund")
def crawl_fund_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact fund name from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="HTTP cache directory.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    max_pages: Annotated[
        int,
        typer.Option(
            "--max-pages",
            min=1,
            max=100,
        ),
    ] = 25,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            min=0,
            max=5,
        ),
    ] = 2,
    max_documents: Annotated[
        int,
        typer.Option(
            "--max-documents",
            min=0,
            max=100,
        ),
    ] = 20,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore cached HTTP responses.",
        ),
    ] = False,
) -> None:
    """Crawl one fund website and download relevant documents."""

    summary: CrawlSummary

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if (fund.name.casefold() == fund_name.casefold())]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        if len(matching_funds) > 1:
            typer.echo(
                f"Fund name is not unique: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        settings = HttpSettings.from_environment()

        async def run_crawler() -> CrawlSummary:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await crawl_fund_site(
                    database_path=database_path,
                    fund=fund,
                    fetcher=fetcher,
                    max_pages=max_pages,
                    max_depth=max_depth,
                    max_documents=max_documents,
                    force=force,
                )

        summary = asyncio.run(run_crawler())
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        FetchError,
        CrawlError,
    ) as exc:
        typer.echo(
            f"Fund crawl failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {summary.fund_name}")
    typer.echo(f"Pages visited: {summary.pages_visited}")
    typer.echo(f"HTML pages parsed: {summary.html_pages_parsed}")
    typer.echo(f"Documents discovered: {summary.documents_discovered}")
    typer.echo(f"Documents downloaded: {summary.documents_downloaded}")
    typer.echo(f"Failures: {len(summary.failures)}")

    for index, document in enumerate(
        summary.document_candidates[:20],
        start=1,
    ):
        typer.echo("")
        typer.echo(f"{index}. [{document.score}] {document.document_type.value}")
        typer.echo(f"   {document.text or '-'}")
        typer.echo(f"   {document.url}")

    if summary.failures:
        typer.echo("")
        typer.echo("Failures:")

        for failure in summary.failures:
            typer.echo(f"- {failure.stage}: {failure.url}: {failure.error_code}")


@app.command("discover-official")
def discover_official_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact fund name from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="HTTP cache directory.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    max_pages: Annotated[
        int,
        typer.Option(
            "--max-pages",
            min=1,
            max=100,
            help="How many official pages may be fetched.",
        ),
    ] = 18,
    max_documents: Annotated[
        int,
        typer.Option(
            "--max-documents",
            min=1,
            max=200,
        ),
    ] = 60,
    run_id: Annotated[
        str,
        typer.Option(
            "--run-id",
            help="Label separating this discovery run in the log.",
        ),
    ] = "",
    show_rejected: Annotated[
        bool,
        typer.Option(
            "--show-rejected",
            help="Also print the links discovery refused.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore cached HTTP responses.",
        ),
    ] = False,
) -> None:
    """Explore the official sources of one fund without any fallback."""

    result: OfficialDiscoveryResult

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if (fund.name.casefold() == fund_name.casefold())]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        settings = HttpSettings.from_environment()

        async def run_discovery() -> OfficialDiscoveryResult:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await discover_official_sources(
                    database_path=database_path,
                    fund=fund,
                    fetcher=fetcher,
                    max_pages=max_pages,
                    max_documents=max_documents,
                    force=force,
                    run_id=run_id,
                )

        result = asyncio.run(run_discovery())
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        FetchError,
    ) as exc:
        typer.echo(
            f"Official discovery failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    metrics = result.metrics

    typer.echo(f"Fund: {result.fund_name}")
    typer.echo(f"Official domain: {result.official_domain}")
    typer.echo(f"Pages inspected: {metrics.pages_inspected}")
    typer.echo(f"Pages fetched: {metrics.pages_fetched}")
    typer.echo(f"Sitemaps read: {metrics.sitemaps_read}")
    typer.echo(f"Documents discovered: {metrics.documents_discovered}")
    typer.echo(f"Documents accepted: {metrics.documents_accepted}")
    typer.echo(f"Documents rejected: {metrics.documents_rejected}")
    typer.echo(f"Official source hits: {metrics.official_source_hits}")
    typer.echo(f"Official exploration complete: {result.is_exhausted}")
    typer.echo(f"Official sources sufficient: {result.is_sufficient}")

    if result.missing_document_groups:
        typer.echo(f"Missing document groups: {', '.join(result.missing_document_groups)}")
        typer.echo("A fallback adapter would still be allowed to run for this fund.")

    typer.echo("Discovery methods:")

    for method, occurrences in metrics.method_counts.items():
        typer.echo(f"- {method}: {occurrences}")

    typer.echo("")
    typer.echo("Accepted documents:")

    for index, document in enumerate(
        result.documents[:25],
        start=1,
    ):
        typer.echo(f"{index}. [{document.score}] {document.document_type.value}")
        typer.echo(f"   {document.text or '-'}")
        typer.echo(f"   {document.url}")

    if show_rejected:
        typer.echo("")
        typer.echo("Rejected links:")

        for entry in result.entries:
            if entry.accepted:
                continue

            typer.echo(f"- [{entry.priority_score}] {entry.rejection_reason}: {entry.url}")

    if result.warnings:
        typer.echo("")
        typer.echo("Warnings:")

        for warning in result.warnings[:20]:
            typer.echo(f"- {warning}")


@app.command("parse-fund-documents")
def parse_fund_documents_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact fund name from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    parsed_directory: Annotated[
        Path,
        typer.Option(
            "--parsed-directory",
            help="Directory for parsed document JSON files.",
            file_okay=False,
        ),
    ] = Path("cache/parsed"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Parse sources already marked as parsed again.",
        ),
    ] = False,
    anydoc_fallback: Annotated[
        bool,
        typer.Option(
            "--anydoc-fallback",
            help=(
                "Read PDFs whose layout defeated the parser a second time "
                "with AnyDoc, and keep that reading only when it is better."
            ),
        ),
    ] = False,
) -> None:
    """Convert downloaded fund documents into normalized text."""

    summary: DocumentParsingSummary

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if (fund.name.casefold() == fund_name.casefold())]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        if len(matching_funds) > 1:
            typer.echo(
                f"Fund name is not unique: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        summary = asyncio.run(
            parse_fund_documents(
                database_path=database_path,
                fund=fund,
                parsed_directory=parsed_directory,
                force=force,
                allow_anydoc_fallback=anydoc_fallback,
            )
        )
    except (
        InputFileError,
        DatabaseError,
        DocumentParseError,
    ) as exc:
        typer.echo(
            f"Document parsing failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {summary.fund_name}")
    typer.echo(f"Sources considered: {summary.sources_considered}")
    typer.echo(f"Documents parsed: {summary.documents_parsed}")
    typer.echo(f"Scanned PDF candidates: {summary.scanned_candidates}")
    typer.echo(f"Extracted characters: {summary.total_characters}")
    typer.echo(f"Failures: {len(summary.failures)}")

    if summary.anydoc_triggered:
        typer.echo(
            f"AnyDoc layout fallback: {summary.anydoc_triggered} triggered, "
            f"{summary.anydoc_accepted} accepted, "
            f"{summary.anydoc_rejected} rejected, "
            f"{summary.anydoc_blocked} blocked, "
            f"{summary.anydoc_failed} failed "
            f"({summary.anydoc_seconds:.1f}s)"
        )

    if summary.failures:
        typer.echo("")
        typer.echo("Parsing failures:")

        for failure in summary.failures:
            typer.echo(
                f"- Source {failure.source_id}: "
                f"{failure.url}: "
                f"{failure.error_code}: "
                f"{failure.message}"
            )


@app.command("extract-fund")
def extract_fund_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact fund name from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Enriched output JSON file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
) -> None:
    """Extract the delivered and the extended fields of one parsed fund."""

    summary: ExtractionSummary

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if (fund.name.casefold() == fund_name.casefold())]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        if len(matching_funds) > 1:
            typer.echo(
                f"Fund name is not unique: {fund_name}",
                err=True,
            )
            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        summary = extract_fund_data(
            database_path=database_path,
            output_path=output_path,
            fund=fund,
        )
    except (
        InputFileError,
        DatabaseError,
        ExtractionServiceError,
    ) as exc:
        typer.echo(
            f"Fund extraction failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {summary.fund_name}")
    typer.echo(f"Parsed documents: {summary.parsed_documents}")
    typer.echo(f"Fields found: {summary.fields_found}/5")
    typer.echo(f"Fields missing: {summary.fields_missing}/5")

    for field_name, status in summary.field_statuses:
        typer.echo(f"- {field_name}: {status.value}")

    if summary.extended_statuses:
        typer.echo("Extended fields (schema version 3):")

        for field_name, status in summary.extended_statuses:
            typer.echo(f"- {field_name}: {status.value}")

        typer.echo(f"Extended rows stored: {summary.extended_rows}")

    typer.echo(f"Warnings: {len(summary.warnings)}")
    typer.echo(f"Output file: {summary.output_path}")


@app.command("run-fund")
def run_fund_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact fund name from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Enriched output JSON file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="HTTP cache directory.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    parsed_directory: Annotated[
        Path,
        typer.Option(
            "--parsed-directory",
            help="Parsed document directory.",
            file_okay=False,
        ),
    ] = Path("cache/parsed"),
    max_pages: Annotated[
        int,
        typer.Option(
            "--max-pages",
            min=1,
            max=100,
        ),
    ] = 25,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            min=0,
            max=5,
        ),
    ] = 2,
    max_documents: Annotated[
        int,
        typer.Option(
            "--max-documents",
            min=0,
            max=100,
        ),
    ] = 20,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore cached and previously parsed data.",
        ),
    ] = False,
) -> None:
    """Run the complete pipeline for one fund."""

    result: FundPipelineResult

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if (fund.name.casefold() == fund_name.casefold())]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )

            raise typer.Exit(code=1)

        if len(matching_funds) > 1:
            typer.echo(
                f"Fund name is not unique: {fund_name}",
                err=True,
            )

            raise typer.Exit(code=1)

        fund = matching_funds[0]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        synchronize_output_file(
            funds=funds,
            output_path=output_path,
        )

        settings = HttpSettings.from_environment()

        async def run_pipeline() -> FundPipelineResult:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await run_fund_pipeline(
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

        result = asyncio.run(run_pipeline())
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        OutputFileError,
    ) as exc:
        typer.echo(
            f"Fund pipeline failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {result.fund_name}")

    typer.echo(f"Domain adapter: {result.adapter_name or '-'}")

    typer.echo(f"Status: {result.status.value}")

    typer.echo(f"Pages visited: {result.pages_visited}")

    typer.echo(f"Documents discovered: {result.documents_discovered}")

    typer.echo(f"Documents downloaded: {result.documents_downloaded}")

    typer.echo(f"Documents parsed: {result.documents_parsed}")

    typer.echo(f"Fields found: {result.fields_found}/5")

    for field_name, status in result.field_statuses:
        typer.echo(f"- {field_name}: {status}")

    if result.extended_statuses:
        typer.echo("Extended fields (schema version 3):")

        for field_name, status in result.extended_statuses:
            typer.echo(f"- {field_name}: {status}")

        typer.echo(f"Extended rows stored: {result.extended_rows}")

    typer.echo(f"Failures: {len(result.failures)}")

    typer.echo(f"Duration seconds: {result.duration_seconds}")

    if result.status is FundStatus.FAILED:
        raise typer.Exit(code=1)


@app.command("run-sample")
def run_sample_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Enriched output JSON file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            help="JSON report for the sample run.",
            dir_okay=False,
        ),
    ] = Path("reports/sample-run.json"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="HTTP cache directory.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    parsed_directory: Annotated[
        Path,
        typer.Option(
            "--parsed-directory",
            help="Parsed document directory.",
            file_okay=False,
        ),
    ] = Path("cache/parsed"),
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            max=230,
            help="Number of funds to process.",
        ),
    ] = 10,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Number of input funds to skip.",
        ),
    ] = 0,
    max_pages: Annotated[
        int,
        typer.Option(
            "--max-pages",
            min=1,
            max=100,
        ),
    ] = 25,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            min=0,
            max=5,
        ),
    ] = 2,
    max_documents: Annotated[
        int,
        typer.Option(
            "--max-documents",
            min=0,
            max=100,
        ),
    ] = 20,
    document_concurrency: Annotated[
        int,
        typer.Option(
            "--document-concurrency",
            min=1,
            max=20,
            help="Maximum concurrent document downloads per fund.",
        ),
    ] = 4,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            max=20,
            help="Maximum funds processed concurrently.",
        ),
    ] = 6,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore cached and parsed data.",
        ),
    ] = False,
    fresh_output: Annotated[
        bool,
        typer.Option(
            "--fresh-output",
            help="Reset all enriched output records to pending.",
        ),
    ] = False,
) -> None:
    """Run the complete pipeline for a sample of input funds."""

    summary: BatchPipelineSummary

    try:
        funds = load_funds(input_path)

        if offset >= len(funds):
            typer.echo(
                f"Sample offset is outside the input fund list: {offset} >= {len(funds)}",
                err=True,
            )

            raise typer.Exit(code=1)

        selected_funds = funds[offset : offset + limit]

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        synchronize_output_file(
            funds=funds,
            output_path=output_path,
            reset=fresh_output,
        )

        settings = HttpSettings.from_environment()

        def show_progress(
            index: int,
            total: int,
            result: FundPipelineResult,
        ) -> None:
            typer.echo(
                f"[{index}/{total}] "
                f"{result.fund_name}: "
                f"{result.status.value}, "
                f"{result.fields_found}/5 fields"
            )

        async def run_batch() -> BatchPipelineSummary:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await run_fund_batch(
                    database_path=database_path,
                    output_path=output_path,
                    parsed_directory=parsed_directory,
                    funds=selected_funds,
                    fetcher=fetcher,
                    max_pages=max_pages,
                    max_depth=max_depth,
                    max_documents=max_documents,
                    document_concurrency=document_concurrency,
                    concurrency=concurrency,
                    force=force,
                    progress_callback=show_progress,
                )

        summary = asyncio.run(run_batch())

        write_batch_report(
            summary=summary,
            report_path=report_path,
        )
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        OutputFileError,
    ) as exc:
        typer.echo(
            f"Sample pipeline failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo("")
    typer.echo(f"Funds requested: {summary.requested}")

    typer.echo(f"Completed 5/5: {summary.completed}")

    typer.echo(f"Partial: {summary.partial}")

    typer.echo(f"Failed: {summary.failed}")

    typer.echo(f"Output file: {summary.output_path}")

    typer.echo(f"Report file: {report_path}")


@app.command("run-retry")
def run_retry_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Retry subset JSON file.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.retry.json"),
    master_input_path: Annotated[
        Path,
        typer.Option(
            "--master-input",
            help="Complete original funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Master enriched output JSON file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    retry_output_path: Annotated[
        Path,
        typer.Option(
            "--retry-output",
            help="Temporary isolated retry output JSON file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.retry.enriched.json"),
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            help="JSON report for the retry run.",
            dir_okay=False,
        ),
    ] = Path("reports/retry-run.json"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="HTTP cache directory.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    parsed_directory: Annotated[
        Path,
        typer.Option(
            "--parsed-directory",
            help="Parsed document directory.",
            file_okay=False,
        ),
    ] = Path("cache/parsed"),
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=0,
            max=230,
            help="Number of retry funds to process; 0 means all.",
        ),
    ] = 0,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Number of retry-input funds to skip.",
        ),
    ] = 0,
    max_pages: Annotated[
        int,
        typer.Option(
            "--max-pages",
            min=1,
            max=100,
        ),
    ] = 30,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            min=0,
            max=5,
        ),
    ] = 3,
    max_documents: Annotated[
        int,
        typer.Option(
            "--max-documents",
            min=0,
            max=100,
        ),
    ] = 30,
    document_concurrency: Annotated[
        int,
        typer.Option(
            "--document-concurrency",
            min=1,
            max=20,
            help="Maximum concurrent document downloads per fund.",
        ),
    ] = 4,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            max=20,
            help="Maximum retry funds processed concurrently.",
        ),
    ] = 6,
    minimum_found_improvement: Annotated[
        int,
        typer.Option(
            "--minimum-found-improvement",
            min=0,
            max=5,
            help=(
                "Minimum increase in found fields required before "
                "a retry result replaces the master result."
            ),
        ),
    ] = 1,
    avant_fallback: Annotated[
        bool,
        typer.Option(
            "--avant-fallback/--no-avant-fallback",
            help=("Use the AVANT catalog as an additional document source."),
        ),
    ] = True,
    amista_fallback: Annotated[
        bool,
        typer.Option(
            "--amista-fallback/--no-amista-fallback",
            help="Use the AMISTA catalog as a cross-domain fund-profile source.",
        ),
    ] = True,
    porovnejfondy_fallback: Annotated[
        bool,
        typer.Option(
            "--porovnejfondy-fallback/--no-porovnejfondy-fallback",
            help="Use PorovnejFondy.cz as an optional final fund-page fallback.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore cached and parsed data.",
        ),
    ] = False,
) -> None:
    """Retry only weak funds and merge only improved results."""

    summary: BatchPipelineSummary
    merge_summary: RetryMergeSummary

    try:
        master_funds = load_funds(master_input_path)

        all_retry_funds = load_funds(input_path)

        if offset >= len(all_retry_funds):
            raise OutputFileError(
                f"Retry offset is outside the retry input: {offset} >= {len(all_retry_funds)}"
            )

        if limit == 0:
            requested_retry_funds = all_retry_funds[offset:]
        else:
            requested_retry_funds = all_retry_funds[offset : offset + limit]

        master_by_id = {stable_fund_id(fund): fund for fund in master_funds}

        requested_retry_ids = [stable_fund_id(fund) for fund in requested_retry_funds]

        retry_fund_ids = set(requested_retry_ids)

        if len(retry_fund_ids) != len(requested_retry_ids):
            raise OutputFileError("Retry input contains duplicate funds.")

        missing_ids = sorted(retry_fund_ids - set(master_by_id))

        if missing_ids:
            raise OutputFileError(
                "Retry input contains funds that are not present "
                "in the master input: " + ", ".join(missing_ids)
            )

        retry_funds = [master_by_id[fund_id] for fund_id in requested_retry_ids]

        if output_path.resolve() == retry_output_path.resolve():
            raise OutputFileError("Master output and retry output must be different files.")

        initialize_database(database_path)

        register_funds(
            database_path,
            master_funds,
        )

        synchronize_output_file(
            funds=master_funds,
            output_path=output_path,
        )

        synchronize_output_file(
            funds=retry_funds,
            output_path=retry_output_path,
            reset=True,
        )

        settings = HttpSettings.from_environment()

        def show_progress(
            index: int,
            total: int,
            result: FundPipelineResult,
        ) -> None:
            typer.echo(
                f"[{index}/{total}] "
                f"{result.fund_name}: "
                f"{result.status.value}, "
                f"{result.fields_found}/5 fields"
            )

        async def run_batch() -> BatchPipelineSummary:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await run_fund_batch(
                    database_path=database_path,
                    output_path=retry_output_path,
                    parsed_directory=parsed_directory,
                    funds=retry_funds,
                    fetcher=fetcher,
                    max_pages=max_pages,
                    max_depth=max_depth,
                    max_documents=max_documents,
                    document_concurrency=document_concurrency,
                    concurrency=concurrency,
                    force=force,
                    avant_fallback=avant_fallback,
                    amista_fallback=amista_fallback,
                    porovnejfondy_fallback=(porovnejfondy_fallback),
                    progress_callback=show_progress,
                )

        summary = asyncio.run(run_batch())

        write_batch_report(
            summary=summary,
            report_path=report_path,
        )

        master_outputs = load_output(output_path)

        retry_outputs = load_output(retry_output_path)

        merged_outputs, merge_summary = merge_improved_outputs(
            base_outputs=master_outputs,
            retry_outputs=retry_outputs,
            retry_fund_ids=retry_fund_ids,
            minimum_found_improvement=(minimum_found_improvement),
        )

        write_output(
            output_path,
            merged_outputs,
            overwrite=True,
        )

        report_payload = json.loads(report_path.read_text(encoding="utf-8"))

        report_payload["merge"] = {
            "minimum_found_improvement": (minimum_found_improvement),
            "avant_fallback": avant_fallback,
            "amista_fallback": amista_fallback,
            "porovnejfondy_fallback": (porovnejfondy_fallback),
            "concurrency": concurrency,
            "document_concurrency": document_concurrency,
            "candidates": merge_summary.candidates,
            "replaced": merge_summary.replaced,
            "preserved": merge_summary.preserved,
            "missing_candidates": (merge_summary.missing_candidates),
            "master_output_path": str(output_path),
            "retry_output_path": str(retry_output_path),
        }

        serialized_report = (
            json.dumps(
                report_payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

        temporary_report_path = report_path.with_suffix(f"{report_path.suffix}.tmp")

        temporary_report_path.write_text(
            serialized_report,
            encoding="utf-8",
        )

        temporary_report_path.replace(report_path)
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        OutputFileError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        typer.echo(
            f"Retry pipeline failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo("")
    typer.echo(f"Retry funds requested: {summary.requested}")
    typer.echo(f"Completed 5/5: {summary.completed}")
    typer.echo(f"Partial: {summary.partial}")
    typer.echo(f"Failed: {summary.failed}")
    typer.echo(f"Master records replaced: {merge_summary.replaced}")
    typer.echo(f"Master records preserved: {merge_summary.preserved}")
    typer.echo(f"Master output: {output_path}")
    typer.echo(f"Retry output: {retry_output_path}")
    typer.echo(f"Report file: {report_path}")


@app.command("plan-fallbacks")
def plan_fallbacks_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    enriched_output_path: Annotated[
        Path,
        typer.Option(
            "--output-data",
            help="Enriched fund output JSON.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    source_config_path: Annotated[
        Path,
        typer.Option(
            "--sources",
            help="Approved fallback source registry.",
            dir_okay=False,
        ),
    ] = Path("config/fallback_sources.json"),
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            help="Fallback plan output JSON.",
            dir_okay=False,
        ),
    ] = Path("reports/fallback-plan.json"),
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            max=230,
            help="Maximum number of input funds to inspect.",
        ),
    ] = None,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
        ),
    ] = 0,
) -> None:
    """Create ordered fallback actions for incomplete funds."""

    try:
        funds = load_funds(input_path)

        if offset >= len(funds):
            typer.echo(
                f"Fallback offset is outside the input fund list: {offset} >= {len(funds)}",
                err=True,
            )

            raise typer.Exit(code=1)

        selected_funds = funds[offset:] if limit is None else funds[offset : offset + limit]

        outputs = load_output(enriched_output_path)

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        approved_sources = load_approved_fallback_sources(source_config_path)

        report = build_fallback_plan(
            funds=selected_funds,
            outputs=outputs,
            database_path=database_path,
            approved_sources=approved_sources,
        )

        write_fallback_plan(
            report=report,
            path=report_path,
        )
    except (
        InputFileError,
        OutputFileError,
        DatabaseError,
        FallbackSourceConfigError,
        FallbackPlanError,
    ) as exc:
        typer.echo(
            f"Fallback planning failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo(f"Funds considered: {report.funds_considered}")

    typer.echo(f"Funds requiring fallback: {report.funds_requiring_fallback}")

    typer.echo("Missing fields:")

    for field_name, count in report.missing_field_counts.items():
        typer.echo(f"- {field_name}: {count}")

    typer.echo(f"Approved sources: {len(approved_sources)}")

    typer.echo(f"Report file: {report_path}")


@app.command("inspect-adapter")
def inspect_adapter_command(
    fund_name: Annotated[
        str,
        typer.Argument(
            help="Exact fund name from funds.json.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
        ),
    ] = False,
) -> None:
    """Inspect domain-specific discovery without running extraction."""

    result: DomainAdapterResult

    try:
        funds = load_funds(input_path)

        matching_funds = [fund for fund in funds if (fund.name.casefold() == fund_name.casefold())]

        if not matching_funds:
            typer.echo(
                f"Fund was not found: {fund_name}",
                err=True,
            )

            raise typer.Exit(code=1)

        fund = matching_funds[0]

        adapter = get_domain_adapter(fund)

        if adapter is None:
            typer.echo(
                f"No domain adapter supports: {fund.web}",
                err=True,
            )

            raise typer.Exit(code=1)

        settings = HttpSettings.from_environment()

        async def run_adapter() -> DomainAdapterResult:
            async with HttpFetcher(
                settings,
                cache_directory,
            ) as fetcher:
                return await adapter.discover(
                    fund=fund,
                    fetcher=fetcher,
                    force=force,
                )

        result = asyncio.run(run_adapter())
    except (
        InputFileError,
        ConfigurationError,
        FetchError,
    ) as exc:
        typer.echo(
            f"Adapter inspection failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo(f"Fund: {fund.name}")

    typer.echo(f"Adapter: {result.adapter_name}")

    typer.echo(f"Navigation URLs: {len(result.navigation_urls)}")

    for url in result.navigation_urls:
        typer.echo(f"- {url}")

    typer.echo(f"Documents: {len(result.documents)}")

    for document in result.documents:
        typer.echo("")
        typer.echo(f"- [{document.score}] {document.document_type.value}")
        typer.echo(f"  {document.text or '-'}")
        typer.echo(f"  {document.url}")

    typer.echo(f"Warnings: {len(result.warnings)}")

    for warning in result.warnings:
        typer.echo(f"- {warning}")


@app.command("build-grounding-packets")
def build_grounding_packets_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Path to funds.json.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    enriched_output_path: Annotated[
        Path,
        typer.Option(
            "--output-data",
            help="Enriched fund output JSON.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            help="Grounding packet output JSON.",
            dir_okay=False,
        ),
    ] = Path("reports/grounding-packets.json"),
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            max=230,
            help="Maximum number of input funds to inspect.",
        ),
    ] = None,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Number of input funds to skip.",
        ),
    ] = 0,
    max_snippets: Annotated[
        int,
        typer.Option(
            "--max-snippets",
            min=1,
            max=30,
            help="Maximum snippets per unresolved field.",
        ),
    ] = 8,
) -> None:
    """Create grounded evidence packets for unresolved fields."""

    report: GroundingBatchReport

    try:
        funds = load_funds(input_path)

        if offset >= len(funds):
            typer.echo(
                f"Grounding offset is outside the input fund list: {offset} >= {len(funds)}",
                err=True,
            )

            raise typer.Exit(code=1)

        selected_funds = funds[offset:] if limit is None else funds[offset : offset + limit]

        outputs = load_output(enriched_output_path)

        initialize_database(database_path)

        register_funds(
            database_path,
            funds,
        )

        report = build_grounding_packets(
            funds=selected_funds,
            outputs=outputs,
            database_path=database_path,
            max_snippets=max_snippets,
        )

        write_grounding_packets(
            report=report,
            path=report_path,
        )

    except (
        InputFileError,
        OutputFileError,
        DatabaseError,
        GroundingPacketError,
        ValueError,
    ) as exc:
        typer.echo(
            f"Grounding packet creation failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo(f"Funds considered: {report.funds_considered}")

    typer.echo(f"Funds with packets: {report.funds_with_packets}")

    typer.echo(f"Packets total: {report.packets_total}")

    typer.echo(f"Packets with context: {report.packets_with_context}")

    typer.echo(f"Packets without context: {report.packets_without_context}")

    typer.echo(f"Report file: {report_path}")


@app.command("apply-grounded-decisions")
def apply_grounded_decisions_command(
    packet_path: Annotated[
        Path,
        typer.Option(
            "--packets",
            help="Grounding packet report JSON.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("reports/grounding-packets.json"),
    decision_path: Annotated[
        Path,
        typer.Option(
            "--decisions",
            help="Grounded provider decision JSON.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("reports/grounded-decisions.json"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Enriched output JSON.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.enriched.json"),
    allow_overwrite: Annotated[
        bool,
        typer.Option(
            "--allow-overwrite",
            help="Allow replacing fields already marked as found.",
        ),
    ] = False,
) -> None:
    """Validate and apply grounded provider decisions."""

    summary: GroundedApplicationSummary

    try:
        packets = load_grounding_packets(packet_path)

        decisions = load_grounded_decisions(decision_path)

        summary = apply_grounded_decisions(
            packet_report=packets,
            decision_file=decisions,
            output_path=output_path,
            allow_overwrite=allow_overwrite,
        )

    except (
        GroundingPacketError,
        GroundedDecisionError,
        OutputFileError,
    ) as exc:
        typer.echo(
            f"Grounded decision application failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    typer.echo(f"Provider: {summary.provider}")

    typer.echo(f"Decisions received: {summary.decisions_received}")

    typer.echo(f"Decisions applied: {summary.decisions_applied}")

    typer.echo(f"Found applied: {summary.found_applied}")

    typer.echo(f"Unresolved applied: {summary.unresolved_applied}")

    typer.echo(f"Funds updated: {summary.funds_updated}")

    typer.echo(f"Output file: {summary.output_path}")


@app.command("two-pass")
def two_pass_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Canonical fund list. Every fund in it is registered.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/input/funds.json"),
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            "-d",
            help="SQLite processing database.",
            dir_okay=False,
        ),
    ] = Path("cache/fundscraper.sqlite3"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Enriched output JSON file.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.full.json"),
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            help="JSON report of the two-pass run.",
            dir_okay=False,
        ),
    ] = Path("reports/step7-two-pass.json"),
    audit_path: Annotated[
        Path | None,
        typer.Option(
            "--audit",
            help="Step 4 audit report used to select deep-pass funds.",
            dir_okay=False,
        ),
    ] = None,
    conflicts_path: Annotated[
        Path | None,
        typer.Option(
            "--conflicts",
            help="Step 8 conflict report used to select deep-pass funds.",
            dir_okay=False,
        ),
    ] = None,
    cache_directory: Annotated[
        Path,
        typer.Option(
            "--cache-directory",
            help="HTTP cache directory, shared by both passes.",
            file_okay=False,
        ),
    ] = Path("cache/http"),
    parsed_directory: Annotated[
        Path,
        typer.Option(
            "--parsed-directory",
            help="Parsed document directory.",
            file_okay=False,
        ),
    ] = Path("cache/parsed"),
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=0,
            help="Crawl only the first N funds. 0 means every fund.",
        ),
    ] = 0,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Number of input funds to skip before crawling.",
        ),
    ] = 0,
    fund_id: Annotated[
        list[str] | None,
        typer.Option(
            "--fund-id",
            help="Crawl only these funds. May be repeated.",
        ),
    ] = None,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            max=20,
            help="Maximum funds processed concurrently.",
        ),
    ] = 6,
    skip_deep_pass: Annotated[
        bool,
        typer.Option(
            "--skip-deep-pass",
            help="Run only the fast pass and report what the deep pass would do.",
        ),
    ] = False,
    deep_limit: Annotated[
        int,
        typer.Option(
            "--deep-limit",
            min=0,
            help=(
                "Crawl at most this many funds in the deep pass, "
                "highest priority first. 0 means every selected fund."
            ),
        ),
    ] = 0,
    progress_every: Annotated[
        int,
        typer.Option(
            "--progress-every",
            min=1,
            help="Print a progress line every N funds.",
        ),
    ] = 10,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Ignore the HTTP cache and refetch everything.",
        ),
    ] = False,
    avant_fallback: Annotated[
        bool,
        typer.Option("--avant-fallback"),
    ] = False,
    amista_fallback: Annotated[
        bool,
        typer.Option("--amista-fallback"),
    ] = False,
    porovnejfondy_fallback: Annotated[
        bool,
        typer.Option("--porovnejfondy-fallback"),
    ] = False,
    anydoc_fallback: Annotated[
        bool,
        typer.Option("--anydoc-fallback"),
    ] = False,
) -> None:
    """
    Crawl every fund cheaply, then crawl the problem funds deeply.

    The canonical fund list is always registered in full, so limiting a
    run to a sample never shrinks the database or the output file to it.
    """

    try:
        canonical_funds = load_funds(input_path.resolve())

        selected_funds = _two_pass_selection(
            funds=canonical_funds,
            fund_ids=list(fund_id or []),
            offset=offset,
            limit=limit,
        )

        if not selected_funds:
            typer.echo(
                "No fund matched the selection.",
                err=True,
            )

            raise typer.Exit(code=1)

        settings = HttpSettings.from_environment()

        started = perf_counter()

        def show_progress(
            crawl_pass: CrawlPass,
            done: int,
            total: int,
            result: FundPipelineResult,
        ) -> None:
            # One line every few funds, never one per request. A run of
            # several hundred funds otherwise buries its own summary.
            if done % progress_every and done != total:
                return

            elapsed = perf_counter() - started

            label = crawl_pass.value.upper()

            line = (
                f"{label} {done}/{total} | fund={result.fund_name[:34]} "
                f"| pages={result.pages_visited} | docs={result.documents_downloaded} "
                f"| elapsed={_duration(elapsed)}"
            )

            if crawl_pass is CrawlPass.DEEP:
                targets = ",".join(
                    sorted(wanted_by_fund.get(result.fund_id, ()))[:4],
                )

                line = (
                    f"{label} {done}/{total} | fund={result.fund_name[:34]} "
                    f"| targets={targets or '-'}"
                )

            typer.echo(line)

        wanted_by_fund: dict[str, tuple[str, ...]] = {}

        async def run() -> tuple[TwoPassSummary, int, int]:
            async with HttpFetcher(
                settings,
                cache_directory.resolve(),
            ) as fetcher:
                summary = await run_two_pass(
                    database_path=database_path.resolve(),
                    output_path=output_path.resolve(),
                    parsed_directory=parsed_directory.resolve(),
                    canonical_funds=canonical_funds,
                    selected_funds=selected_funds,
                    fetcher=fetcher,
                    audit_findings=_report_section(
                        audit_path,
                        "findings",
                    ),
                    conflict_records=_report_section(
                        conflicts_path,
                        "conflicts",
                    ),
                    skip_deep_pass=skip_deep_pass,
                    deep_limit=deep_limit,
                    progress=show_progress,
                    concurrency=concurrency,
                    force=force,
                    avant_fallback=avant_fallback,
                    amista_fallback=amista_fallback,
                    porovnejfondy_fallback=porovnejfondy_fallback,
                    anydoc_fallback=anydoc_fallback,
                )

                return (
                    summary,
                    fetcher.cache_hits,
                    fetcher.cache_misses,
                )

        summary, cache_hits, cache_misses = asyncio.run(run())

        write_two_pass_report(
            summary=summary,
            report_path=report_path.resolve(),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )
    except (
        InputFileError,
        DatabaseError,
        ConfigurationError,
        OutputFileError,
    ) as exc:
        typer.echo(
            f"Two-pass run failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    fast = summary.fast_metrics

    deep = summary.deep_metrics

    runtime = (summary.finished_at - summary.started_at).total_seconds()

    typer.echo("")
    typer.echo(f"Runtime: {_duration(runtime)}")
    typer.echo(f"Canonical funds registered: {summary.canonical_funds}")
    typer.echo(
        f"Fast pass:  {fast.funds} funds, {fast.pages_visited} pages, "
        f"{fast.documents_downloaded} documents"
    )
    typer.echo(f"Deep pass selected: {len(summary.plans)} funds")
    typer.echo(
        f"Deep pass executed: {deep.funds} funds, {deep.pages_visited} pages, "
        f"{deep.documents_downloaded} documents"
    )
    typer.echo(
        f"Superseded copies skipped: {fast.superseded_documents + deep.superseded_documents}"
    )
    typer.echo(
        f"Expected sitemap misses: {fast.expected_misses + deep.expected_misses}"
        f" | warnings: {fast.warnings + deep.warnings}"
        f" | real failures: {fast.real_failures + deep.real_failures}"
    )
    typer.echo(f"HTTP cache: {cache_hits} hits, {cache_misses} misses")
    typer.echo(f"Recovered fields: {len(summary.recovered_fields)}")

    stages = ", ".join(
        f"{name}={_duration(value)}"
        for name, value in (
            ("discovery", fast.discovery_seconds + deep.discovery_seconds),
            ("crawl", fast.crawl_seconds + deep.crawl_seconds),
            ("parse", fast.parse_seconds + deep.parse_seconds),
            ("extract", fast.extract_seconds + deep.extract_seconds),
        )
    )

    typer.echo(f"Fund time by stage: {stages}")

    slowest = sorted(
        summary.fast_results + summary.deep_results,
        key=lambda item: -item.duration_seconds,
    )[:5]

    if slowest:
        typer.echo("")
        typer.echo("Slowest funds:")

        for item in slowest:
            typer.echo(
                f"  {_duration(item.duration_seconds):>8}  {item.fund_name[:44]:46s}"
                f" parse={_duration(item.timings.parse)}"
                f" extract={_duration(item.timings.extract)}"
            )

    triggers: Counter[str] = Counter(
        reason.trigger.value for plan in summary.plans for reason in plan.reasons
    )

    if triggers:
        typer.echo("")
        typer.echo("Top deep-pass trigger reasons:")

        for trigger, count in triggers.most_common(5):
            typer.echo(f"  {trigger:32s}{count}")

    typer.echo("")
    typer.echo(f"Output file: {summary.output_path}")
    typer.echo(f"Report file: {report_path}")


def _duration(
    seconds: float,
) -> str:
    """Return a duration a reader can compare at a glance."""

    minutes, remaining = divmod(
        int(seconds),
        60,
    )

    hours, minutes = divmod(
        minutes,
        60,
    )

    if hours:
        return f"{hours}h{minutes:02d}m"

    if minutes:
        return f"{minutes}m{remaining:02d}s"

    return f"{remaining}s"


def _two_pass_selection(
    *,
    funds: list[FundInput],
    fund_ids: list[str],
    offset: int,
    limit: int,
) -> list[FundInput]:
    """Return the funds this run crawls, out of the canonical list."""

    if fund_ids:
        wanted = set(fund_ids)

        return [fund for fund in funds if stable_fund_id(fund) in wanted]

    chosen = funds[offset:]

    return chosen[:limit] if limit > 0 else chosen


def _report_section(
    path: Path | None,
    key: str,
) -> list[dict[str, object]]:
    """Read one array out of a report, or nothing when it is absent."""

    if path is None or not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []

    section = payload.get(key) if isinstance(payload, dict) else None

    if not isinstance(section, list):
        return []

    return [item for item in section if isinstance(item, dict)]


@app.command("export-delivery")
def export_delivery_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            help="Internal auditable output to convert.",
            dir_okay=False,
            readable=True,
        ),
    ] = Path("data/output/funds.full.json"),
    output_path: Annotated[
        Path,
        typer.Option(
            "--output",
            "-o",
            help="Clean delivery JSON to write.",
            dir_okay=False,
        ),
    ] = Path("data/output/funds.delivery.json"),
    audit_path: Annotated[
        Path | None,
        typer.Option(
            "--audit",
            help=(
                "Audit report. Any field the audit doubts is delivered as "
                "not_found. Required unless --unsafe-without-audit is given."
            ),
            dir_okay=False,
        ),
    ] = None,
    unsafe_without_audit: Annotated[
        bool,
        typer.Option(
            "--unsafe-without-audit",
            help=(
                "Export on the extraction status alone. Suspicious, "
                "conflicting and rejected values are then delivered as "
                "found. For development only."
            ),
        ),
    ] = False,
    include_source_url: Annotated[
        bool,
        typer.Option(
            "--source-url/--no-source-url",
            help="Keep the document address of a delivered value.",
        ),
    ] = True,
) -> None:
    """
    Write the clean delivery JSON derived from the internal output.

    The internal file is only read. Each field becomes a status and a
    value; nothing about how the value was found crosses over.
    """

    resolved_input = input_path.resolve()

    resolved_output = output_path.resolve()

    if audit_path is None and not unsafe_without_audit:
        # A value can be found and still be wrong in a way only the audit
        # knows about — an implausible per-share figure delivered as fund
        # capital, a series dated in the future. Exporting without the
        # audit hands those to a reader as clean data, so it has to be
        # asked for explicitly.
        typer.echo(
            "An audit report is required for a safe delivery export. "
            "Produce one with scripts/audit_enriched_output.py and pass "
            "--audit, or pass --unsafe-without-audit to export on the "
            "extraction status alone.",
            err=True,
        )

        raise typer.Exit(code=1)

    if resolved_input == resolved_output:
        typer.echo(
            "The delivery output must not overwrite the internal output.",
            err=True,
        )

        raise typer.Exit(code=1)

    try:
        records = load_records(resolved_input)

        audit = load_audit(
            audit_path.resolve() if audit_path is not None else None,
        )

        if audit_path is not None:
            # The audit is only worth applying to the file it was made
            # from. Every run of this project produces the same fund
            # identifiers, so an overlap of names proves nothing; the
            # digest of the audited bytes does.
            mismatch = audit_mismatch_reason(
                audit=audit,
                records=records,
                input_sha256=input_digest(resolved_input),
            )

            if mismatch is not None:
                typer.echo(
                    f"The audit report does not describe this input: {mismatch}. "
                    "Re-run scripts/audit_enriched_output.py against this exact "
                    "file before exporting.",
                    err=True,
                )

                raise typer.Exit(code=1)

        findings = list(audit.findings)

        delivered = build_delivery_records(
            records=records,
            audit_findings=findings,
            include_source_url=include_source_url,
        )

        write_delivery_output(
            records=delivered,
            path=resolved_output,
        )
    except DeliveryExportError as exc:
        typer.echo(
            f"Delivery export failed: {exc}",
            err=True,
        )

        raise typer.Exit(code=1) from exc

    counts = delivery_summary(delivered)

    typer.echo(f"Internal input: {resolved_input}")
    typer.echo(f"Delivery output: {resolved_output}")
    typer.echo(f"Funds: {len(delivered)}")
    typer.echo(
        "Audit applied: "
        + (
            f"{audit_path} ({len(findings)} findings)"
            if audit_path
            else "NO - exported without an audit, values may be unsafe"
        )
    )
    typer.echo("")
    typer.echo("Delivered values per field:")

    for field, count in counts.items():
        typer.echo(f"  {field:26s}{count:>6}")
