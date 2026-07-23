from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from fundscraper.cli import app

runner = CliRunner()


def write_input(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Example SICAV a.s.",
                    "web": "https://example.com",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_validate_input_command(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "validate-input",
            str(input_path),
        ],
    )

    assert result.exit_code == 0
    assert "Funds: 1" in result.stdout
    assert "Unique domains: 1" in result.stdout
    assert "Input validation passed." in result.stdout


def test_validate_input_command_reports_failure(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.json"

    result = runner.invoke(
        app,
        [
            "validate-input",
            str(missing_path),
        ],
    )

    assert result.exit_code == 1
    assert "Input validation failed:" in result.output


def test_init_and_validate_output_commands(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"
    output_path = tmp_path / "funds.enriched.json"

    write_input(input_path)

    init_result = runner.invoke(
        app,
        [
            "init-output",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
    )

    validate_result = runner.invoke(
        app,
        [
            "validate-output",
            str(output_path),
        ],
    )

    assert init_result.exit_code == 0, init_result.output
    assert "Funds initialized: 1" in init_result.stdout

    assert validate_result.exit_code == 0, validate_result.output
    assert "Output validation passed." in validate_result.stdout


def test_generate_schema_command(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "schema.json"

    result = runner.invoke(
        app,
        [
            "generate-schema",
            "--output",
            str(schema_path),
        ],
    )

    assert result.exit_code == 0
    assert schema_path.exists()
    assert "Schema file:" in result.stdout


def test_database_commands(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    database_path = tmp_path / "fundscraper.sqlite3"

    write_input(input_path)

    init_result = runner.invoke(
        app,
        [
            "init-db",
            "--input",
            str(input_path),
            "--database",
            str(database_path),
        ],
    )

    status_result = runner.invoke(
        app,
        [
            "db-status",
            "--database",
            str(database_path),
        ],
    )

    validate_result = runner.invoke(
        app,
        [
            "validate-db",
            str(database_path),
        ],
    )

    assert init_result.exit_code == 0, init_result.output
    assert "Funds registered: 1" in init_result.stdout
    assert "Database initialization passed." in init_result.stdout

    assert status_result.exit_code == 0, status_result.output
    assert "Funds total: 1" in status_result.stdout
    assert "Funds pending: 1" in status_result.stdout

    assert validate_result.exit_code == 0, validate_result.output
    assert "Database validation passed." in validate_result.stdout


def test_discover_start_page_reports_missing_fund(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "discover-start-page",
            "Unknown Fund",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1
    assert "Fund was not found:" in result.output


def test_crawl_fund_reports_missing_fund(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "crawl-fund",
            "Unknown Fund",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1
    assert "Fund was not found:" in result.output


def test_parse_fund_documents_reports_missing_fund(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "parse-fund-documents",
            "Unknown Fund",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1
    assert "Fund was not found:" in result.output


def test_extract_fund_reports_missing_fund(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "extract-fund",
            "Unknown Fund",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1
    assert "Fund was not found:" in result.output


def test_run_fund_reports_missing_fund(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "run-fund",
            "Unknown Fund",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1

    assert "Fund was not found:" in result.output


def test_run_sample_rejects_invalid_offset(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "run-sample",
            "--input",
            str(input_path),
            "--offset",
            "100",
        ],
    )

    assert result.exit_code == 1

    assert "Sample offset is outside" in result.output


def test_inspect_adapter_reports_missing_fund(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.json"

    write_input(input_path)

    result = runner.invoke(
        app,
        [
            "inspect-adapter",
            "Unknown Fund",
            "--input",
            str(input_path),
        ],
    )

    assert result.exit_code == 1

    assert "Fund was not found:" in result.output
