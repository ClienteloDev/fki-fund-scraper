from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fundscraper.database import (
    DatabaseError,
    get_database_status,
    initialize_database,
    register_funds,
    reset_database,
    validate_database,
)
from fundscraper.models import FundInput

FIXED_TIME = datetime(
    2026,
    7,
    23,
    12,
    0,
    tzinfo=UTC,
)


def sample_funds() -> list[FundInput]:
    return [
        FundInput(
            name="Example SICAV a.s.",
            web="https://example.com",
        ),
        FundInput(
            name="Second SICAV a.s.",
            web="https://fund.example.cz",
        ),
    ]


def test_initialize_register_and_validate_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(
        database_path,
        now=FIXED_TIME,
    )

    registered_count = register_funds(
        database_path,
        sample_funds(),
        now=FIXED_TIME,
    )

    status = get_database_status(database_path)

    validate_database(database_path)

    assert registered_count == 2
    assert status.schema_version == 1
    assert status.funds_total == 2
    assert status.funds_pending == 2
    assert status.sources_total == 0
    assert status.attempts_total == 0


def test_register_funds_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(
        database_path,
        now=FIXED_TIME,
    )

    register_funds(
        database_path,
        sample_funds(),
        now=FIXED_TIME,
    )

    register_funds(
        database_path,
        sample_funds(),
        now=FIXED_TIME,
    )

    status = get_database_status(database_path)

    assert status.funds_total == 2
    assert status.funds_pending == 2


def test_database_status_rejects_missing_file(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "missing.sqlite3"

    with pytest.raises(
        DatabaseError,
        match="does not exist",
    ):
        get_database_status(database_path)


def test_reset_database_removes_related_files(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    wal_path = Path(f"{database_path}-wal")

    shm_path = Path(f"{database_path}-shm")

    database_path.write_text(
        "database",
        encoding="utf-8",
    )

    wal_path.write_text(
        "wal",
        encoding="utf-8",
    )

    shm_path.write_text(
        "shm",
        encoding="utf-8",
    )

    reset_database(database_path)

    assert not database_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()
