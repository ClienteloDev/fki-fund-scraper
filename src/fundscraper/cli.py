from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer

from fundscraper.input_loader import InputFileError, load_funds

app = typer.Typer(
    name="fundscraper",
    help="Enrich Czech qualified investor fund data from public sources.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """FKI fund data enrichment command-line application."""


def canonical_domain(url: str) -> str:
    """Return a normalized hostname used for input statistics."""

    hostname = urlparse(url).hostname

    if hostname is None:
        return ""

    return hostname.lower().removeprefix("www.")


def canonical_url(url: str) -> str:
    """Return a lightweight normalized URL used for duplicate detection."""

    return url.strip().rstrip("/").lower()


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
        typer.echo(f"Input validation failed: {exc}", err=True)
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
