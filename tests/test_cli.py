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

    assert init_result.exit_code == 0
    assert "Funds initialized: 1" in init_result.stdout

    assert validate_result.exit_code == 0
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
