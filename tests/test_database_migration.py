"""
Migration and persistence tests for schema version 3.

The new tables are additive, so a database created by version 2 must be
upgraded by creating them, without touching the data it already holds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from fundscraper.database import (
    SCHEMA_VERSION,
    AnnualReturnRecord,
    CapitalObservationRecord,
    FundNewsRecord,
    FundPartyRecord,
    HistoricalValueRecord,
    get_database_status,
    initialize_database,
    list_annual_returns,
    list_capital_observations,
    list_fund_news,
    list_fund_parties,
    list_historical_values,
    record_annual_return,
    record_capital_observation,
    record_fund_news,
    record_historical_value,
    register_funds,
    upsert_fund_party,
    validate_database,
)
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id

FUND = FundInput(
    name="EXAMPLE fond SICAV, a.s.",
    web="https://www.examplefond.cz/",
)


# The tables of schema version 2, before the extended results existed.
VERSION_TWO_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funds (
    fund_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    web TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    document_type TEXT,
    content_type TEXT,
    title TEXT,
    published_at TEXT,
    retrieved_at TEXT,
    http_status INTEGER,
    sha256 TEXT,
    local_path TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (fund_id, canonical_url)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parsed_documents (
    source_id INTEGER PRIMARY KEY,
    fund_id TEXT NOT NULL,
    document_format TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    character_count INTEGER NOT NULL,
    scanned_candidate INTEGER NOT NULL,
    text_path TEXT NOT NULL,
    parsed_at TEXT NOT NULL
);
"""


EXTENDED_TABLES = (
    "fund_parties",
    "capital_observations",
    "annual_returns",
    "historical_values",
    "fund_news",
)


def create_version_two_database(
    path: Path,
) -> None:
    connection = sqlite3.connect(path)

    try:
        connection.executescript(VERSION_TWO_SQL)

        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES (2, '2026-01-01T00:00:00+00:00')",
        )

        connection.execute(
            """
            INSERT INTO funds (
                fund_id, name, web, canonical_url, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'partial', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00')
            """,
            (
                stable_fund_id(FUND),
                FUND.name,
                FUND.web,
                "https://examplefond.cz/",
            ),
        )

        connection.commit()
    finally:
        connection.close()


def table_names(
    path: Path,
) -> set[str]:
    connection = sqlite3.connect(path)

    try:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()


def test_version_two_database_is_upgraded_without_losing_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"

    create_version_two_database(database_path)

    assert not (table_names(database_path) & set(EXTENDED_TABLES))

    initialize_database(database_path)

    assert set(EXTENDED_TABLES) <= table_names(database_path)

    status = get_database_status(database_path)

    assert status.schema_version == SCHEMA_VERSION

    # The fund the old database held is still there, with its status.
    assert status.funds_total == 1
    assert status.funds_partial == 1

    validate_database(database_path)


def test_initialization_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(database_path)
    initialize_database(database_path)

    assert get_database_status(database_path).schema_version == SCHEMA_VERSION


def prepared_database(
    tmp_path: Path,
) -> tuple[Path, str]:
    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [FUND],
    )

    return (
        database_path,
        stable_fund_id(FUND),
    )


def test_fund_party_round_trip(
    tmp_path: Path,
) -> None:
    database_path, fund_id = prepared_database(tmp_path)

    record = FundPartyRecord(
        fund_id=fund_id,
        role="manager",
        name="AMISTA investicni spolecnost, a.s.",
        legal_name="AMISTA investicni spolecnost, a.s.",
        ico="27437558",
        web="https://www.amista.cz/",
        source_url="https://www.examplefond.cz/statut.pdf",
        retrieved_at="2026-01-15T10:00:00+00:00",
        confidence="high",
        review_required=False,
    )

    upsert_fund_party(database_path, record)

    assert list_fund_parties(database_path, fund_id=fund_id) == [record]

    # The same role is replaced, not duplicated.
    upsert_fund_party(
        database_path,
        replace(record, name="AVANT investicni spolecnost, a.s."),
    )

    stored = list_fund_parties(database_path, fund_id=fund_id)

    assert len(stored) == 1
    assert stored[0].name == "AVANT investicni spolecnost, a.s."


def test_capital_observation_round_trip_and_deduplication(
    tmp_path: Path,
) -> None:
    database_path, fund_id = prepared_database(tmp_path)

    def observation(
        metric_type: str,
        amount: float,
        as_of: str,
    ) -> CapitalObservationRecord:
        return CapitalObservationRecord(
            fund_id=fund_id,
            metric_type=metric_type,
            amount=amount,
            currency="CZK",
            as_of=as_of,
            share_class=None,
            scope_type="fund",
            source_url="https://www.examplefond.cz/vz-2025.pdf",
            quote="Fondovy kapital: 693 601 745 Kc",
            page=12,
            confidence="high",
            review_required=False,
        )

    record_capital_observation(
        database_path, observation("fund_capital", 693_601_745, "2025-12-31")
    )
    record_capital_observation(
        database_path, observation("fund_capital", 500_000_000, "2024-12-31")
    )
    record_capital_observation(
        database_path,
        observation("registered_capital", 2_000_000, "2025-12-31"),
    )

    stored = list_capital_observations(database_path, fund_id=fund_id)

    assert len(stored) == 3

    # Storing the same metric, date and currency replaces the amount.
    record_capital_observation(
        database_path, observation("fund_capital", 700_000_000, "2025-12-31")
    )

    fund_capital = list_capital_observations(
        database_path,
        fund_id=fund_id,
        metric_type="fund_capital",
    )

    assert len(fund_capital) == 2
    assert fund_capital[-1].amount == 700_000_000

    # Series order is chronological, ready for a graph.
    assert [item.as_of for item in fund_capital] == ["2024-12-31", "2025-12-31"]


def test_annual_returns_separate_series_types_and_classes(
    tmp_path: Path,
) -> None:
    database_path, fund_id = prepared_database(tmp_path)

    def annual_return(
        year: int,
        percent: float,
        series_type: str = "calendar_year",
        share_class: str | None = "A",
    ) -> AnnualReturnRecord:
        return AnnualReturnRecord(
            fund_id=fund_id,
            year=year,
            series_type=series_type,
            return_percent=percent,
            share_class=share_class,
            currency="CZK",
            source_url="https://www.examplefond.cz/vz-2025.pdf",
            quote="2024: 7,2 %",
            page=3,
            confidence="high",
            review_required=False,
        )

    record_annual_return(database_path, annual_return(2024, 7.2))
    record_annual_return(database_path, annual_return(2024, 9.9, share_class="B"))
    record_annual_return(database_path, annual_return(2024, 21.0, series_type="cumulative"))

    stored = list_annual_returns(database_path, fund_id=fund_id)

    # The same year of a different class or series is a separate row.
    assert len(stored) == 3

    calendar_a = [
        item for item in stored if item.series_type == "calendar_year" and item.share_class == "A"
    ]

    assert len(calendar_a) == 1
    assert calendar_a[0].return_percent == 7.2


def test_historical_values_keep_series_apart(
    tmp_path: Path,
) -> None:
    database_path, fund_id = prepared_database(tmp_path)

    def value(
        as_of: str,
        amount: float,
        value_type: str = "nav_per_share",
        share_class: str | None = "A",
        currency: str = "CZK",
    ) -> HistoricalValueRecord:
        return HistoricalValueRecord(
            fund_id=fund_id,
            as_of=as_of,
            value_type=value_type,
            value=amount,
            currency=currency,
            share_class=share_class,
            unit="per share",
            frequency="annual",
            source_url="https://www.examplefond.cz/vz-2025.pdf",
            quote="1,2512 Kc",
            page=4,
            confidence="medium",
            review_required=False,
        )

    record_historical_value(database_path, value("2025-12-31", 1.2512))
    record_historical_value(database_path, value("2024-12-31", 1.1138))
    record_historical_value(database_path, value("2025-12-31", 5_612.96, share_class="PIA"))
    record_historical_value(
        database_path,
        value("2025-12-31", 250_000_000, value_type="fund_net_assets"),
    )

    stored = list_historical_values(database_path, fund_id=fund_id)

    assert len(stored) == 4

    nav_a = [
        item for item in stored if item.value_type == "nav_per_share" and item.share_class == "A"
    ]

    assert [item.as_of for item in nav_a] == ["2024-12-31", "2025-12-31"]


def test_fund_news_round_trip(
    tmp_path: Path,
) -> None:
    database_path, fund_id = prepared_database(tmp_path)

    record = FundNewsRecord(
        fund_id=fund_id,
        url="https://www.examplefond.cz/aktuality/nova-nemovitost",
        title="Fond koupil novou nemovitost",
        published_at="2026-02-01",
        summary="Fond rozsiril portfolio.",
        source_domain="examplefond.cz",
        source_type="official_fund",
        relation_confidence="high",
    )

    record_fund_news(database_path, record)
    record_fund_news(database_path, record)

    stored = list_fund_news(database_path, fund_id=fund_id)

    assert stored == [record]
