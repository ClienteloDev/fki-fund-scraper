from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from fundscraper.database import (
    DatabaseError,
    get_database_status,
    initialize_database,
    register_funds,
    reset_database,
    validate_database,
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
