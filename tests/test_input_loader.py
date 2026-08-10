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


PROJECT_INPUT_PATH = Path("data/input/funds.json")


def read_project_input() -> list[dict[str, object]]:
    payload = json.loads(PROJECT_INPUT_PATH.read_text(encoding="utf-8-sig"))

    assert isinstance(payload, list)

    return payload


def test_project_input_has_the_expected_record_structure() -> None:
    """
    The fund list grows as new funds are added, so the number of records
    is not asserted. Every record must carry exactly a name and a website,
    where the website is either a valid HTTP address or still unknown.
    """

    records = read_project_input()

    assert records

    for index, record in enumerate(records):
        assert isinstance(record, dict), index

        assert set(record) == {"name", "web"}, index

        name = record["name"]

        assert isinstance(name, str) and name.strip(), index

        web = record["web"]

        assert web is None or isinstance(web, str), index

        if isinstance(web, str) and web:
            assert web.startswith(("http://", "https://")), index


def test_project_input_names_are_unique() -> None:
    records = read_project_input()

    names = [str(record["name"]).strip() for record in records]

    assert len(names) == len(set(names))


def test_project_funds_with_a_website_load_and_keep_their_order(
    tmp_path: Path,
) -> None:
    """
    The strict input model requires a website, so records still awaiting
    a domain are excluded. Everything else must load unchanged.
    """

    records = read_project_input()

    with_website = [record for record in records if record["web"]]

    assert with_website

    input_path = tmp_path / "funds.json"

    write_json(
        input_path,
        with_website,
    )

    funds = load_funds(input_path)

    assert len(funds) == len(with_website)

    assert [fund.name for fund in funds] == [str(record["name"]).strip() for record in with_website]

    assert [fund.web for fund in funds] == [str(record["web"]) for record in with_website]


def test_a_fund_without_a_website_loads(
    tmp_path: Path,
) -> None:
    """One fund with no known site must not reject the whole dataset."""

    path = tmp_path / "funds.json"

    path.write_text(
        json.dumps(
            [
                {
                    "name": "Rezidento Alfa SICAV, a.s.",
                    "web": "https://www.rezidentoalfa.cz",
                },
                {
                    "name": "FestLen SICAV a.s.",
                    "web": None,
                },
            ]
        ),
        encoding="utf-8",
    )

    funds = load_funds(path)

    assert len(funds) == 2

    assert funds[0].has_website

    assert not funds[1].has_website

    assert funds[1].web is None


def test_a_malformed_website_is_still_rejected(
    tmp_path: Path,
) -> None:
    """Allowing an absent site must not allow an invalid one."""

    path = tmp_path / "funds.json"

    path.write_text(
        json.dumps(
            [
                {
                    "name": "Rezidento Alfa SICAV, a.s.",
                    "web": "ftp://www.rezidentoalfa.cz",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(InputFileError):
        load_funds(path)


def test_the_canonical_dataset_loads_and_is_counted_dynamically() -> None:
    """The count comes from the file, never from a constant."""

    canonical = Path("data/input/funds.json")

    if not canonical.exists():
        pytest.skip("canonical dataset is not present")

    funds = load_funds(canonical)

    assert funds

    assert len(funds) == len({fund.name for fund in funds})
