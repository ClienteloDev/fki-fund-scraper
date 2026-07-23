from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from fundscraper.cli import app

runner = CliRunner()


def test_validate_input_command(tmp_path: Path) -> None:
    input_path = tmp_path / "funds.json"

    input_path.write_text(
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


def test_validate_input_command_reports_failure(tmp_path: Path) -> None:
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
