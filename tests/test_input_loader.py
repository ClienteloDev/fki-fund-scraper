from __future__ import annotations

import json
from pathlib import Path

import pytest

from fundscraper.input_loader import InputFileError, load_funds


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_loads_valid_funds(tmp_path: Path) -> None:
    input_path = tmp_path / "funds.json"

    write_json(
        input_path,
        [
            {
                "name": "Example SICAV a.s.",
                "web": "https://example.com",
            },
            {
                "name": "Second Fund SICAV a.s.",
                "web": "http://fund.example.cz/",
            },
        ],
    )

    funds = load_funds(input_path)

    assert len(funds) == 2
    assert funds[0].name == "Example SICAV a.s."
    assert funds[0].web == "https://example.com"
    assert funds[1].name == "Second Fund SICAV a.s."


def test_rejects_missing_file(tmp_path: Path) -> None:
    input_path = tmp_path / "missing.json"

    with pytest.raises(InputFileError, match="does not exist"):
        load_funds(input_path)


def test_rejects_invalid_json(tmp_path: Path) -> None:
    input_path = tmp_path / "funds.json"
    input_path.write_text('[{"name": "Broken"}', encoding="utf-8")

    with pytest.raises(InputFileError, match="invalid JSON"):
        load_funds(input_path)


def test_rejects_non_array_root(tmp_path: Path) -> None:
    input_path = tmp_path / "funds.json"

    write_json(
        input_path,
        {
            "name": "Example SICAV a.s.",
            "web": "https://example.com",
        },
    )

    with pytest.raises(InputFileError, match="root JSON value must be an array"):
        load_funds(input_path)


def test_rejects_invalid_url_protocol(tmp_path: Path) -> None:
    input_path = tmp_path / "funds.json"

    write_json(
        input_path,
        [
            {
                "name": "Example SICAV a.s.",
                "web": "ftp://example.com",
            }
        ],
    )

    with pytest.raises(InputFileError, match="http or https"):
        load_funds(input_path)


def test_rejects_unknown_fields(tmp_path: Path) -> None:
    input_path = tmp_path / "funds.json"

    write_json(
        input_path,
        [
            {
                "name": "Example SICAV a.s.",
                "web": "https://example.com",
                "unexpected": "value",
            }
        ],
    )

    with pytest.raises(InputFileError, match="Extra inputs are not permitted"):
        load_funds(input_path)


def test_project_input_contains_230_funds() -> None:
    funds = load_funds(Path("data/input/funds.json"))

    assert len(funds) == 230
