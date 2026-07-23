from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from fundscraper.config import (
    ConfigurationError,
    HttpSettings,
)
from fundscraper.crawl_service import (
    CrawlError,
    CrawlSummary,
    crawl_fund_site,
)
from fundscraper.database import (
    DatabaseError,
    get_database_status,
    initialize_database,
    register_funds,
    reset_database,
    validate_database,
)
from fundscraper.discovery_service import (
    DiscoverySummary,
    discover_fund_start_page,
)
from fundscraper.fetch_service import (
    fetch_fund_start_page,
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
from fundscraper.normalization import canonical_domain, canonical_url
from fundscraper.output_service import (
    OutputFileError,
    create_pending_output,
    load_output,
    write_output,
    write_output_schema,
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

    domains = Counter(canonical_domain(fund.web) for fund in funds)

    canonical_urls = [canonical_url(fund.web) for fund in funds]

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
    ] = Path("docs/funds-output.schema.json"),
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
