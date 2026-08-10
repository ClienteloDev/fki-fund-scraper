from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fundscraper.database import (
    SCHEMA_VERSION,
    AttemptStatus,
    DatabaseError,
    FundStatus,
    SourceStatus,
    get_database_status,
    initialize_database,
    record_attempt,
    register_funds,
    reset_database,
    update_fund_status,
    upsert_source,
    validate_database,
)
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id

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
    assert status.schema_version == SCHEMA_VERSION
    assert status.funds_total == 2
    assert status.funds_pending == 2
    assert status.sources_total == 0
    assert status.attempts_total == 0
    assert status.parsed_documents_total == 0


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


def test_records_attempt_and_source(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    funds = sample_funds()

    initialize_database(
        database_path,
        now=FIXED_TIME,
    )

    register_funds(
        database_path,
        funds,
        now=FIXED_TIME,
    )

    fund_id = stable_fund_id(funds[0])

    attempt_id = record_attempt(
        database_path,
        fund_id=fund_id,
        stage="fetch_start_page",
        status=AttemptStatus.SUCCEEDED,
        url=funds[0].web,
        now=FIXED_TIME,
    )

    source_id = upsert_source(
        database_path,
        fund_id=fund_id,
        url=funds[0].web or "",
        status=SourceStatus.DOWNLOADED,
        document_type="marketing_page",
        content_type="text/html",
        retrieved_at=FIXED_TIME,
        http_status=200,
        sha256="a" * 64,
        local_path="cache/example.body",
        now=FIXED_TIME,
    )

    status = get_database_status(database_path)

    assert attempt_id > 0
    assert source_id > 0
    assert status.attempts_total == 1
    assert status.sources_total == 1


def test_register_funds_removes_stale_records(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    funds = sample_funds()

    initialize_database(
        database_path,
        now=FIXED_TIME,
    )

    register_funds(
        database_path,
        funds,
        now=FIXED_TIME,
    )

    initial_status = get_database_status(database_path)

    assert initial_status.funds_total == 2

    register_funds(
        database_path,
        [funds[0]],
        now=FIXED_TIME,
    )

    synchronized_status = get_database_status(database_path)

    assert synchronized_status.funds_total == 1
    assert synchronized_status.funds_pending == 1


def test_updates_fund_processing_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    funds = sample_funds()

    initialize_database(
        database_path,
        now=FIXED_TIME,
    )

    register_funds(
        database_path,
        funds,
        now=FIXED_TIME,
    )

    fund_id = stable_fund_id(funds[0])

    update_fund_status(
        database_path,
        fund_id=fund_id,
        status=FundStatus.COMPLETED,
        now=FIXED_TIME,
    )

    status = get_database_status(database_path)

    assert status.funds_total == 2
    assert status.funds_completed == 1
    assert status.funds_pending == 1
