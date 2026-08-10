from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from fundscraper.models import FundInput
from fundscraper.normalization import canonical_url
from fundscraper.output_service import stable_fund_id

SCHEMA_VERSION: Final = 5


class FundStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceStatus(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    PARSED = "parsed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AttemptStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


SCHEMA_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funds (
    fund_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    web TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'in_progress',
                'completed',
                'partial',
                'failed'
            )
        ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funds_status
ON funds(status);

CREATE INDEX IF NOT EXISTS idx_funds_canonical_url
ON funds(canonical_url);

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
    sha256 TEXT
        CHECK (
            sha256 IS NULL
            OR length(sha256) = 64
        ),
    local_path TEXT,
    status TEXT NOT NULL
        CHECK (
            status IN (
                'discovered',
                'downloaded',
                'parsed',
                'failed',
                'skipped'
            )
        ),
    error_code TEXT,
    error_message TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE,
    UNIQUE (fund_id, canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_sources_fund_id
ON sources(fund_id);

CREATE INDEX IF NOT EXISTS idx_sources_status
ON sources(status);

CREATE INDEX IF NOT EXISTS idx_sources_document_type
ON sources(document_type);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL
        CHECK (
            status IN (
                'started',
                'succeeded',
                'failed',
                'skipped'
            )
        ),
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attempts_fund_id
ON attempts(fund_id);

CREATE INDEX IF NOT EXISTS idx_attempts_stage
ON attempts(stage);

CREATE INDEX IF NOT EXISTS idx_attempts_status
ON attempts(status);
CREATE TABLE IF NOT EXISTS parsed_documents (
    source_id INTEGER PRIMARY KEY,
    fund_id TEXT NOT NULL,
    document_format TEXT NOT NULL
        CHECK (
            document_format IN (
                'pdf',
                'html',
                'xhtml',
                'xml',
                'text'
            )
        ),
    parser_name TEXT NOT NULL,
    page_count INTEGER NOT NULL
        CHECK (page_count >= 0),
    character_count INTEGER NOT NULL
        CHECK (character_count >= 0),
    scanned_candidate INTEGER NOT NULL
        CHECK (
            scanned_candidate IN (0, 1)
        ),
    text_path TEXT NOT NULL,
    parsed_at TEXT NOT NULL,
    FOREIGN KEY (source_id)
        REFERENCES sources(source_id)
        ON DELETE CASCADE,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_parsed_documents_fund_id
ON parsed_documents(fund_id);

CREATE INDEX IF NOT EXISTS idx_parsed_documents_scanned
ON parsed_documents(scanned_candidate);

-- Schema version 3 adds the extended result tables. They are additive,
-- so a database created by an earlier version is upgraded by creating
-- them; no existing table or column changes.

CREATE TABLE IF NOT EXISTS fund_parties (
    fund_id TEXT NOT NULL,
    role TEXT NOT NULL
        CHECK (
            role IN (
                'manager',
                'administrator',
                'depositary',
                'auditor'
            )
        ),
    name TEXT NOT NULL,
    legal_name TEXT,
    ico TEXT
        CHECK (
            ico IS NULL
            OR length(ico) = 8
        ),
    web TEXT,
    source_url TEXT,
    retrieved_at TEXT,
    confidence TEXT,
    review_required INTEGER NOT NULL DEFAULT 0
        CHECK (review_required IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (fund_id, role),
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS capital_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    metric_type TEXT NOT NULL,
    amount REAL NOT NULL
        CHECK (amount >= 0),
    currency TEXT NOT NULL
        CHECK (length(currency) = 3),
    as_of TEXT NOT NULL,
    -- Part of the uniqueness key. SQLite treats NULL values as distinct,
    -- so the absence of a share class is stored as an empty string.
    share_class TEXT NOT NULL DEFAULT '',
    scope_type TEXT,
    source_url TEXT,
    quote TEXT,
    page INTEGER,
    confidence TEXT,
    review_required INTEGER NOT NULL DEFAULT 0
        CHECK (review_required IN (0, 1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE,
    UNIQUE (fund_id, metric_type, as_of, share_class, currency)
);

CREATE INDEX IF NOT EXISTS idx_capital_observations_fund
ON capital_observations(fund_id, metric_type, as_of);

CREATE TABLE IF NOT EXISTS annual_returns (
    return_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    year INTEGER NOT NULL
        CHECK (year BETWEEN 1900 AND 2100),
    series_type TEXT NOT NULL,
    return_percent REAL NOT NULL,
    share_class TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT ''
        CHECK (
            currency = ''
            OR length(currency) = 3
        ),
    source_url TEXT,
    quote TEXT,
    page INTEGER,
    confidence TEXT,
    review_required INTEGER NOT NULL DEFAULT 0
        CHECK (review_required IN (0, 1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE,
    UNIQUE (fund_id, year, series_type, share_class, currency)
);

CREATE INDEX IF NOT EXISTS idx_annual_returns_fund
ON annual_returns(fund_id, year);

CREATE TABLE IF NOT EXISTS historical_values (
    value_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value REAL NOT NULL,
    currency TEXT NOT NULL
        CHECK (length(currency) = 3),
    share_class TEXT NOT NULL DEFAULT '',
    unit TEXT,
    frequency TEXT NOT NULL,
    source_url TEXT,
    quote TEXT,
    page INTEGER,
    confidence TEXT,
    review_required INTEGER NOT NULL DEFAULT 0
        CHECK (review_required IN (0, 1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE,
    UNIQUE (fund_id, as_of, value_type, share_class, currency)
);

CREATE INDEX IF NOT EXISTS idx_historical_values_series
ON historical_values(fund_id, value_type, share_class, as_of);

CREATE TABLE IF NOT EXISTS fund_news (
    news_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT,
    summary TEXT,
    source_domain TEXT NOT NULL,
    source_type TEXT NOT NULL,
    relation_confidence TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE,
    UNIQUE (fund_id, url)
);

CREATE INDEX IF NOT EXISTS idx_fund_news_fund
ON fund_news(fund_id, published_at);

-- Schema version 4 records what discovery saw. Every page and document
-- is written here whether it was accepted or refused, so a fund with a
-- missing document can be explained without crawling it again. The table
-- is additive, so a version 3 database is upgraded by creating it.

CREATE TABLE IF NOT EXISTS discovery_log (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fund_id TEXT NOT NULL,
    url TEXT NOT NULL,
    discovered_from TEXT,
    method TEXT NOT NULL,
    priority_score INTEGER NOT NULL DEFAULT 0,
    document_type TEXT,
    title TEXT,
    scope_decision TEXT,
    accepted INTEGER NOT NULL DEFAULT 0
        CHECK (accepted IN (0, 1)),
    rejection_reason TEXT,
    is_document INTEGER NOT NULL DEFAULT 0
        CHECK (is_document IN (0, 1)),
    run_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE,
    UNIQUE (fund_id, run_id, url)
);

CREATE INDEX IF NOT EXISTS idx_discovery_log_fund
ON discovery_log(fund_id, accepted, priority_score);

-- Schema version 5 records what was learned about each parsed document
-- before any value was read out of it: what kind of document it is, which
-- entity it describes, the dates it carries and how it was parsed. The
-- table is additive, so a version 4 database is upgraded by creating it.

CREATE TABLE IF NOT EXISTS document_metadata (
    source_id INTEGER PRIMARY KEY,
    fund_id TEXT NOT NULL,
    document_type TEXT,
    type_score INTEGER NOT NULL DEFAULT 0,
    type_ambiguous INTEGER NOT NULL DEFAULT 0
        CHECK (type_ambiguous IN (0, 1)),
    type_evidence TEXT,
    scope TEXT,
    scope_confidence TEXT,
    scope_accepted INTEGER NOT NULL DEFAULT 0
        CHECK (scope_accepted IN (0, 1)),
    scope_rejection_reason TEXT,
    identity_evidence TEXT,
    subfund_name TEXT,
    share_class TEXT,
    matched_ico TEXT,
    matched_isin TEXT,
    published_at TEXT,
    published_at_origin TEXT,
    effective_at TEXT,
    reporting_period_start TEXT,
    reporting_period_end TEXT,
    as_of TEXT,
    parser_name TEXT,
    ocr_used INTEGER NOT NULL DEFAULT 0
        CHECK (ocr_used IN (0, 1)),
    pages_parsed INTEGER NOT NULL DEFAULT 0,
    parse_warnings TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (fund_id)
        REFERENCES funds(fund_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_metadata_fund
ON document_metadata(fund_id, document_type, scope);
"""


class DatabaseError(RuntimeError):
    """Raised when the processing database operation fails."""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: int
    fund_id: str
    url: str
    document_type: str | None
    content_type: str | None
    local_path: str | None
    status: SourceStatus


@dataclass(frozen=True, slots=True)
class ParsedDocumentRecord:
    source_id: int
    fund_id: str
    url: str
    title: str | None
    document_type: str | None
    content_type: str | None
    retrieved_at: str | None
    document_format: str
    parser_name: str
    page_count: int
    character_count: int
    scanned_candidate: bool
    text_path: str
    parsed_at: str

    # Where the downloaded body of the source is stored. News items are
    # read from the original HTML, because the plain text of a listing
    # page keeps the headlines but loses the link of every article.
    local_path: str | None = None


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    schema_version: int
    funds_total: int
    funds_pending: int
    funds_in_progress: int
    funds_completed: int
    funds_partial: int
    funds_failed: int
    sources_total: int
    attempts_total: int
    parsed_documents_total: int


def utc_now_iso(
    now: datetime | None = None,
) -> str:
    """Return an ISO 8601 UTC timestamp."""

    current_time = now or datetime.now(UTC)

    if current_time.tzinfo is None:
        raise DatabaseError("Database timestamps must contain timezone information")

    return current_time.astimezone(UTC).isoformat()


def connect_database(path: Path) -> sqlite3.Connection:
    """Open a configured SQLite database connection."""

    connection: sqlite3.Connection | None = None

    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        connection = sqlite3.connect(
            path,
            timeout=30.0,
        )

        connection.execute("PRAGMA foreign_keys = ON")

        connection.execute("PRAGMA busy_timeout = 30000")

        connection.execute("PRAGMA journal_mode = WAL")

        return connection
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()

        raise DatabaseError(f"Could not open database {path}: {exc}") from exc


def initialize_database(
    path: Path,
    *,
    now: datetime | None = None,
) -> None:
    """Create the database schema and register its version."""

    applied_at = utc_now_iso(now)

    try:
        with closing(connect_database(path)) as connection, connection:
            connection.executescript(SCHEMA_SQL)

            current_version = _schema_version(connection)

            if current_version > SCHEMA_VERSION:
                raise DatabaseError(
                    "Database schema is newer than "
                    "this application supports: "
                    f"{current_version} > {SCHEMA_VERSION}"
                )

            if current_version < SCHEMA_VERSION:
                connection.execute(
                    """
                        INSERT INTO schema_migrations (
                            version,
                            applied_at
                        )
                        VALUES (?, ?)
                        """,
                    (
                        SCHEMA_VERSION,
                        applied_at,
                    ),
                )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not initialize database {path}: {exc}") from exc


def register_funds(
    path: Path,
    funds: list[FundInput],
    *,
    now: datetime | None = None,
) -> int:
    """
    Synchronize input funds with the processing database.

    Existing matching funds retain their processing state.
    Records no longer present in the input are removed together with
    their dependent sources and attempts.
    """

    timestamp = utc_now_iso(now)

    rows = [
        (
            stable_fund_id(fund),
            fund.name,
            # The column has always been NOT NULL and rebuilding the
            # table to change that would put every existing row at risk.
            # A fund whose website is unknown is stored with an empty
            # one, which is falsy everywhere the value is read.
            fund.web or "",
            canonical_url(fund.web) if fund.web else "",
            "pending",
            timestamp,
            timestamp,
        )
        for fund in funds
    ]

    current_fund_ids = [(row[0],) for row in rows]

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            with connection:
                connection.execute(
                    """
                    CREATE TEMP TABLE IF NOT EXISTS current_input_funds (
                        fund_id TEXT PRIMARY KEY
                    )
                    """
                )

                connection.execute(
                    """
                    DELETE FROM current_input_funds
                    """
                )

                connection.executemany(
                    """
                    INSERT INTO current_input_funds (
                        fund_id
                    )
                    VALUES (?)
                    """,
                    current_fund_ids,
                )

                connection.executemany(
                    """
                    INSERT INTO funds (
                        fund_id,
                        name,
                        web,
                        canonical_url,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (fund_id)
                    DO UPDATE SET
                        name = excluded.name,
                        web = excluded.web,
                        canonical_url = excluded.canonical_url,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )

                connection.execute(
                    """
                    DELETE FROM funds
                    WHERE fund_id NOT IN (
                        SELECT fund_id
                        FROM current_input_funds
                    )
                    """
                )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not synchronize funds in {path}: {exc}") from exc

    return len(rows)


def record_attempt(
    path: Path,
    *,
    fund_id: str,
    stage: str,
    status: AttemptStatus,
    url: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> int:
    """Record one processing attempt event."""

    timestamp = utc_now_iso(now)

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO attempts (
                        fund_id,
                        stage,
                        url,
                        status,
                        error_code,
                        error_message,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fund_id,
                        stage,
                        url,
                        status.value,
                        error_code,
                        error_message,
                        timestamp,
                    ),
                )

                attempt_id = cursor.lastrowid

                if attempt_id is None:
                    raise DatabaseError("Database did not return an attempt ID")

                return attempt_id
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not record processing attempt in {path}: {exc}") from exc


def upsert_source(
    path: Path,
    *,
    fund_id: str,
    url: str,
    status: SourceStatus,
    document_type: str | None = None,
    content_type: str | None = None,
    title: str | None = None,
    published_at: str | None = None,
    retrieved_at: datetime | None = None,
    http_status: int | None = None,
    sha256: str | None = None,
    local_path: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> int:
    """Insert or update a discovered source for one fund."""

    timestamp = utc_now_iso(now)

    retrieved_at_value = utc_now_iso(retrieved_at) if retrieved_at is not None else None

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            with connection:
                connection.execute(
                    """
                    INSERT INTO sources (
                        fund_id,
                        url,
                        canonical_url,
                        document_type,
                        content_type,
                        title,
                        published_at,
                        retrieved_at,
                        http_status,
                        sha256,
                        local_path,
                        status,
                        error_code,
                        error_message,
                        discovered_at,
                        updated_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT (fund_id, canonical_url)
                    DO UPDATE SET
                        url = excluded.url,
                        document_type = excluded.document_type,
                        content_type = excluded.content_type,
                        title = excluded.title,
                        published_at = excluded.published_at,
                        retrieved_at = excluded.retrieved_at,
                        http_status = excluded.http_status,
                        sha256 = excluded.sha256,
                        local_path = excluded.local_path,
                        status = excluded.status,
                        error_code = excluded.error_code,
                        error_message = excluded.error_message,
                        updated_at = excluded.updated_at
                    """,
                    (
                        fund_id,
                        url,
                        canonical_url(url),
                        document_type,
                        content_type,
                        title,
                        published_at,
                        retrieved_at_value,
                        http_status,
                        sha256,
                        local_path,
                        status.value,
                        error_code,
                        error_message,
                        timestamp,
                        timestamp,
                    ),
                )

                row = connection.execute(
                    """
                    SELECT source_id
                    FROM sources
                    WHERE fund_id = ?
                      AND canonical_url = ?
                    """,
                    (
                        fund_id,
                        canonical_url(url),
                    ),
                ).fetchone()

                if row is None or not isinstance(
                    row[0],
                    int,
                ):
                    raise DatabaseError("Database did not return a source ID")

                return row[0]
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not store source in {path}: {exc}") from exc


def list_parseable_sources(
    path: Path,
    *,
    fund_id: str,
    include_parsed: bool = False,
) -> list[SourceRecord]:
    """Return downloaded sources that can be converted to text."""

    statuses = (
        (
            SourceStatus.DOWNLOADED.value,
            SourceStatus.PARSED.value,
        )
        if include_parsed
        else (SourceStatus.DOWNLOADED.value,)
    )

    placeholders = ", ".join("?" for _ in statuses)

    query = f"""
        SELECT
            source_id,
            fund_id,
            url,
            document_type,
            content_type,
            local_path,
            status
        FROM sources
        WHERE fund_id = ?
          AND status IN ({placeholders})
        ORDER BY source_id
    """

    parameters: tuple[object, ...] = (
        fund_id,
        *statuses,
    )

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            rows = connection.execute(
                query,
                parameters,
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not list parseable sources from {path}: {exc}") from exc

    return [
        SourceRecord(
            source_id=int(row[0]),
            fund_id=str(row[1]),
            url=str(row[2]),
            document_type=(str(row[3]) if row[3] is not None else None),
            content_type=(str(row[4]) if row[4] is not None else None),
            local_path=(str(row[5]) if row[5] is not None else None),
            status=SourceStatus(str(row[6])),
        )
        for row in rows
    ]


def record_parsed_document(
    path: Path,
    *,
    source_id: int,
    fund_id: str,
    document_format: str,
    parser_name: str,
    page_count: int,
    character_count: int,
    scanned_candidate: bool,
    text_path: str,
    now: datetime | None = None,
) -> None:
    """Store parsed document metadata and mark its source as parsed."""

    timestamp = utc_now_iso(now)

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            with connection:
                connection.execute(
                    """
                    INSERT INTO parsed_documents (
                        source_id,
                        fund_id,
                        document_format,
                        parser_name,
                        page_count,
                        character_count,
                        scanned_candidate,
                        text_path,
                        parsed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id)
                    DO UPDATE SET
                        fund_id = excluded.fund_id,
                        document_format = excluded.document_format,
                        parser_name = excluded.parser_name,
                        page_count = excluded.page_count,
                        character_count = excluded.character_count,
                        scanned_candidate = excluded.scanned_candidate,
                        text_path = excluded.text_path,
                        parsed_at = excluded.parsed_at
                    """,
                    (
                        source_id,
                        fund_id,
                        document_format,
                        parser_name,
                        page_count,
                        character_count,
                        int(scanned_candidate),
                        text_path,
                        timestamp,
                    ),
                )

                connection.execute(
                    """
                    UPDATE sources
                    SET
                        status = 'parsed',
                        updated_at = ?
                    WHERE source_id = ?
                    """,
                    (
                        timestamp,
                        source_id,
                    ),
                )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not record parsed document in {path}: {exc}") from exc


def list_parsed_documents(
    path: Path,
    *,
    fund_id: str,
) -> list[ParsedDocumentRecord]:
    """Return parsed documents available for field extraction."""

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            rows = connection.execute(
                """
                SELECT
                    parsed.source_id,
                    parsed.fund_id,
                    sources.url,
                    sources.title,
                    sources.document_type,
                    sources.content_type,
                    sources.retrieved_at,
                    parsed.document_format,
                    parsed.parser_name,
                    parsed.page_count,
                    parsed.character_count,
                    parsed.scanned_candidate,
                    parsed.text_path,
                    parsed.parsed_at,
                    sources.local_path
                FROM parsed_documents AS parsed
                INNER JOIN sources
                    ON sources.source_id = parsed.source_id
                WHERE parsed.fund_id = ?
                ORDER BY parsed.source_id
                """,
                (fund_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not list parsed documents from {path}: {exc}") from exc

    return [
        ParsedDocumentRecord(
            source_id=int(row[0]),
            fund_id=str(row[1]),
            url=str(row[2]),
            title=(str(row[3]) if row[3] is not None else None),
            document_type=(str(row[4]) if row[4] is not None else None),
            content_type=(str(row[5]) if row[5] is not None else None),
            retrieved_at=(str(row[6]) if row[6] is not None else None),
            document_format=str(row[7]),
            parser_name=str(row[8]),
            page_count=int(row[9]),
            character_count=int(row[10]),
            scanned_candidate=bool(row[11]),
            text_path=str(row[12]),
            parsed_at=str(row[13]),
            local_path=(str(row[14]) if row[14] is not None else None),
        )
        for row in rows
    ]


def update_fund_status(
    path: Path,
    *,
    fund_id: str,
    status: FundStatus,
    now: datetime | None = None,
) -> None:
    """Update the processing status of one registered fund."""

    timestamp = utc_now_iso(now)

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            with connection:
                cursor = connection.execute(
                    """
                    UPDATE funds
                    SET
                        status = ?,
                        updated_at = ?
                    WHERE fund_id = ?
                    """,
                    (
                        status.value,
                        timestamp,
                        fund_id,
                    ),
                )

                if cursor.rowcount != 1:
                    raise DatabaseError(
                        f"Fund does not exist in the processing database: {fund_id}"
                    )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not update fund status in {path}: {exc}") from exc


def get_database_status(
    path: Path,
) -> DatabaseStatus:
    """Return processing counts stored in the database."""

    if not path.exists():
        raise DatabaseError(f"Database file does not exist: {path}")

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            return DatabaseStatus(
                schema_version=_schema_version(connection),
                funds_total=_count_rows(
                    connection,
                    "SELECT COUNT(*) FROM funds",
                ),
                funds_pending=_count_rows(
                    connection,
                    """
                    SELECT COUNT(*)
                    FROM funds
                    WHERE status = 'pending'
                    """,
                ),
                funds_in_progress=_count_rows(
                    connection,
                    """
                    SELECT COUNT(*)
                    FROM funds
                    WHERE status = 'in_progress'
                    """,
                ),
                funds_completed=_count_rows(
                    connection,
                    """
                    SELECT COUNT(*)
                    FROM funds
                    WHERE status = 'completed'
                    """,
                ),
                funds_partial=_count_rows(
                    connection,
                    """
                    SELECT COUNT(*)
                    FROM funds
                    WHERE status = 'partial'
                    """,
                ),
                funds_failed=_count_rows(
                    connection,
                    """
                    SELECT COUNT(*)
                    FROM funds
                    WHERE status = 'failed'
                    """,
                ),
                sources_total=_count_rows(
                    connection,
                    "SELECT COUNT(*) FROM sources",
                ),
                attempts_total=_count_rows(
                    connection,
                    "SELECT COUNT(*) FROM attempts",
                ),
                parsed_documents_total=_count_rows(
                    connection,
                    "SELECT COUNT(*) FROM parsed_documents",
                ),
            )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not read database status from {path}: {exc}") from exc


def validate_database(
    path: Path,
) -> None:
    """Run SQLite integrity and foreign-key checks."""

    if not path.exists():
        raise DatabaseError(f"Database file does not exist: {path}")

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            quick_check = connection.execute("PRAGMA quick_check").fetchone()

            if quick_check is None or quick_check[0] != "ok":
                raise DatabaseError(f"SQLite quick_check failed: {quick_check}")

            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

            if foreign_key_errors:
                raise DatabaseError(f"SQLite foreign_key_check failed: {foreign_key_errors}")
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not validate database {path}: {exc}") from exc


def reset_database(
    path: Path,
) -> None:
    """Delete the SQLite database and its temporary WAL files."""

    related_paths = (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    )

    for related_path in related_paths:
        try:
            related_path.unlink(missing_ok=True)
        except OSError as exc:
            raise DatabaseError(f"Could not remove database file {related_path}: {exc}") from exc


def _ensure_initialized(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'schema_migrations'
        """
    ).fetchone()

    if row is None:
        raise DatabaseError(f"Database is not initialized: {path}")


def _schema_version(
    connection: sqlite3.Connection,
) -> int:
    row = connection.execute(
        """
        SELECT MAX(version)
        FROM schema_migrations
        """
    ).fetchone()

    if row is None or row[0] is None:
        return 0

    value = row[0]

    if not isinstance(value, int):
        raise DatabaseError("Database schema version is not an integer")

    return value


def _count_rows(
    connection: sqlite3.Connection,
    query: str,
) -> int:
    row = connection.execute(query).fetchone()

    if row is None:
        raise DatabaseError("Database count query returned no result")

    value = row[0]

    if not isinstance(value, int):
        raise DatabaseError("Database count result is not an integer")

    return value


@dataclass(frozen=True, slots=True)
class FundPartyRecord:
    fund_id: str
    role: str
    name: str
    legal_name: str | None
    ico: str | None
    web: str | None
    source_url: str | None
    retrieved_at: str | None
    confidence: str | None
    review_required: bool


@dataclass(frozen=True, slots=True)
class CapitalObservationRecord:
    fund_id: str
    metric_type: str
    amount: float
    currency: str
    as_of: str
    share_class: str | None
    scope_type: str | None
    source_url: str | None
    quote: str | None
    page: int | None
    confidence: str | None
    review_required: bool


@dataclass(frozen=True, slots=True)
class AnnualReturnRecord:
    fund_id: str
    year: int
    series_type: str
    return_percent: float
    share_class: str | None
    currency: str | None
    source_url: str | None
    quote: str | None
    page: int | None
    confidence: str | None
    review_required: bool


@dataclass(frozen=True, slots=True)
class HistoricalValueRecord:
    fund_id: str
    as_of: str
    value_type: str
    value: float
    currency: str
    share_class: str | None
    unit: str | None
    frequency: str
    source_url: str | None
    quote: str | None
    page: int | None
    confidence: str | None
    review_required: bool


@dataclass(frozen=True, slots=True)
class FundNewsRecord:
    fund_id: str
    url: str
    title: str
    published_at: str | None
    summary: str | None
    source_domain: str
    source_type: str
    relation_confidence: str


def upsert_fund_party(
    path: Path,
    record: FundPartyRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store the manager or administrator acting for one fund."""

    _execute_write(
        path,
        statement="""
            INSERT INTO fund_parties (
                fund_id, role, name, legal_name, ico, web,
                source_url, retrieved_at, confidence, review_required, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fund_id, role) DO UPDATE SET
                name = excluded.name,
                legal_name = excluded.legal_name,
                ico = excluded.ico,
                web = excluded.web,
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at,
                confidence = excluded.confidence,
                review_required = excluded.review_required,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.fund_id,
            record.role,
            record.name,
            record.legal_name,
            record.ico,
            record.web,
            record.source_url,
            record.retrieved_at,
            record.confidence,
            int(record.review_required),
            utc_now_iso(now),
        ),
        description="fund party",
    )


def list_fund_parties(
    path: Path,
    *,
    fund_id: str,
) -> list[FundPartyRecord]:
    """Return the companies acting for one fund."""

    return [
        FundPartyRecord(
            fund_id=str(row[0]),
            role=str(row[1]),
            name=str(row[2]),
            legal_name=_optional_text(row[3]),
            ico=_optional_text(row[4]),
            web=_optional_text(row[5]),
            source_url=_optional_text(row[6]),
            retrieved_at=_optional_text(row[7]),
            confidence=_optional_text(row[8]),
            review_required=bool(row[9]),
        )
        for row in _select_rows(
            path,
            statement="""
                SELECT fund_id, role, name, legal_name, ico, web,
                       source_url, retrieved_at, confidence, review_required
                FROM fund_parties
                WHERE fund_id = ?
                ORDER BY role
                """,
            parameters=(fund_id,),
            description="fund parties",
        )
    ]


def record_capital_observation(
    path: Path,
    record: CapitalObservationRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store one dated capital figure of a fund."""

    _execute_write(
        path,
        statement="""
            INSERT INTO capital_observations (
                fund_id, metric_type, amount, currency, as_of, share_class,
                scope_type, source_url, quote, page, confidence,
                review_required, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fund_id, metric_type, as_of, share_class, currency)
            DO UPDATE SET
                amount = excluded.amount,
                scope_type = excluded.scope_type,
                source_url = excluded.source_url,
                quote = excluded.quote,
                page = excluded.page,
                confidence = excluded.confidence,
                review_required = excluded.review_required,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.fund_id,
            record.metric_type,
            record.amount,
            record.currency,
            record.as_of,
            _key_text(record.share_class),
            record.scope_type,
            record.source_url,
            record.quote,
            record.page,
            record.confidence,
            int(record.review_required),
            utc_now_iso(now),
        ),
        description="capital observation",
    )


def list_capital_observations(
    path: Path,
    *,
    fund_id: str,
    metric_type: str | None = None,
) -> list[CapitalObservationRecord]:
    """Return the capital figures stored for one fund, oldest first."""

    statement = """
        SELECT fund_id, metric_type, amount, currency, as_of, share_class,
               scope_type, source_url, quote, page, confidence, review_required
        FROM capital_observations
        WHERE fund_id = ?
        """

    parameters: tuple[object, ...] = (fund_id,)

    if metric_type is not None:
        statement += " AND metric_type = ?"

        parameters = (
            fund_id,
            metric_type,
        )

    statement += " ORDER BY as_of, metric_type"

    return [
        CapitalObservationRecord(
            fund_id=str(row[0]),
            metric_type=str(row[1]),
            amount=float(row[2]),
            currency=str(row[3]),
            as_of=str(row[4]),
            share_class=_optional_key(row[5]),
            scope_type=_optional_text(row[6]),
            source_url=_optional_text(row[7]),
            quote=_optional_text(row[8]),
            page=int(row[9]) if row[9] is not None else None,
            confidence=_optional_text(row[10]),
            review_required=bool(row[11]),
        )
        for row in _select_rows(
            path,
            statement=statement,
            parameters=parameters,
            description="capital observations",
        )
    ]


def record_annual_return(
    path: Path,
    record: AnnualReturnRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store the performance of one fund in one period."""

    _execute_write(
        path,
        statement="""
            INSERT INTO annual_returns (
                fund_id, year, series_type, return_percent, share_class, currency,
                source_url, quote, page, confidence, review_required, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fund_id, year, series_type, share_class, currency)
            DO UPDATE SET
                return_percent = excluded.return_percent,
                source_url = excluded.source_url,
                quote = excluded.quote,
                page = excluded.page,
                confidence = excluded.confidence,
                review_required = excluded.review_required,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.fund_id,
            record.year,
            record.series_type,
            record.return_percent,
            _key_text(record.share_class),
            _key_text(record.currency),
            record.source_url,
            record.quote,
            record.page,
            record.confidence,
            int(record.review_required),
            utc_now_iso(now),
        ),
        description="annual return",
    )


def list_annual_returns(
    path: Path,
    *,
    fund_id: str,
) -> list[AnnualReturnRecord]:
    """Return the reported performance of one fund, oldest year first."""

    return [
        AnnualReturnRecord(
            fund_id=str(row[0]),
            year=int(row[1]),
            series_type=str(row[2]),
            return_percent=float(row[3]),
            share_class=_optional_key(row[4]),
            currency=_optional_key(row[5]),
            source_url=_optional_text(row[6]),
            quote=_optional_text(row[7]),
            page=int(row[8]) if row[8] is not None else None,
            confidence=_optional_text(row[9]),
            review_required=bool(row[10]),
        )
        for row in _select_rows(
            path,
            statement="""
                SELECT fund_id, year, series_type, return_percent, share_class,
                       currency, source_url, quote, page, confidence, review_required
                FROM annual_returns
                WHERE fund_id = ?
                ORDER BY year, series_type
                """,
            parameters=(fund_id,),
            description="annual returns",
        )
    ]


def record_historical_value(
    path: Path,
    record: HistoricalValueRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store one dated point of a fund value series."""

    _execute_write(
        path,
        statement="""
            INSERT INTO historical_values (
                fund_id, as_of, value_type, value, currency, share_class, unit,
                frequency, source_url, quote, page, confidence,
                review_required, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fund_id, as_of, value_type, share_class, currency)
            DO UPDATE SET
                value = excluded.value,
                unit = excluded.unit,
                frequency = excluded.frequency,
                source_url = excluded.source_url,
                quote = excluded.quote,
                page = excluded.page,
                confidence = excluded.confidence,
                review_required = excluded.review_required,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.fund_id,
            record.as_of,
            record.value_type,
            record.value,
            record.currency,
            _key_text(record.share_class),
            record.unit,
            record.frequency,
            record.source_url,
            record.quote,
            record.page,
            record.confidence,
            int(record.review_required),
            utc_now_iso(now),
        ),
        description="historical value",
    )


def list_historical_values(
    path: Path,
    *,
    fund_id: str,
) -> list[HistoricalValueRecord]:
    """Return the value series points stored for one fund."""

    return [
        HistoricalValueRecord(
            fund_id=str(row[0]),
            as_of=str(row[1]),
            value_type=str(row[2]),
            value=float(row[3]),
            currency=str(row[4]),
            share_class=_optional_key(row[5]),
            unit=_optional_text(row[6]),
            frequency=str(row[7]),
            source_url=_optional_text(row[8]),
            quote=_optional_text(row[9]),
            page=int(row[10]) if row[10] is not None else None,
            confidence=_optional_text(row[11]),
            review_required=bool(row[12]),
        )
        for row in _select_rows(
            path,
            statement="""
                SELECT fund_id, as_of, value_type, value, currency, share_class,
                       unit, frequency, source_url, quote, page, confidence,
                       review_required
                FROM historical_values
                WHERE fund_id = ?
                ORDER BY value_type, share_class, as_of
                """,
            parameters=(fund_id,),
            description="historical values",
        )
    ]


def record_fund_news(
    path: Path,
    record: FundNewsRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store one news item related to a fund."""

    _execute_write(
        path,
        statement="""
            INSERT INTO fund_news (
                fund_id, url, title, published_at, summary, source_domain,
                source_type, relation_confidence, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fund_id, url) DO UPDATE SET
                title = excluded.title,
                published_at = excluded.published_at,
                summary = excluded.summary,
                source_domain = excluded.source_domain,
                source_type = excluded.source_type,
                relation_confidence = excluded.relation_confidence,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.fund_id,
            record.url,
            record.title,
            record.published_at,
            record.summary,
            record.source_domain,
            record.source_type,
            record.relation_confidence,
            utc_now_iso(now),
        ),
        description="fund news",
    )


def list_fund_news(
    path: Path,
    *,
    fund_id: str,
) -> list[FundNewsRecord]:
    """Return the news items stored for one fund, newest first."""

    return [
        FundNewsRecord(
            fund_id=str(row[0]),
            url=str(row[1]),
            title=str(row[2]),
            published_at=_optional_text(row[3]),
            summary=_optional_text(row[4]),
            source_domain=str(row[5]),
            source_type=str(row[6]),
            relation_confidence=str(row[7]),
        )
        for row in _select_rows(
            path,
            statement="""
                SELECT fund_id, url, title, published_at, summary,
                       source_domain, source_type, relation_confidence
                FROM fund_news
                WHERE fund_id = ?
                ORDER BY published_at DESC, title
                """,
            parameters=(fund_id,),
            description="fund news",
        )
    ]


@dataclass(frozen=True, slots=True)
class DiscoveryLogRecord:
    """One page or document discovery saw, accepted or refused."""

    fund_id: str
    url: str
    method: str
    discovered_from: str | None = None
    priority_score: int = 0
    document_type: str | None = None
    title: str | None = None
    scope_decision: str | None = None
    accepted: bool = False
    rejection_reason: str | None = None
    is_document: bool = False
    run_id: str = ""


def record_discovery_entry(
    path: Path,
    record: DiscoveryLogRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store what discovery decided about one URL."""

    _execute_write(
        path,
        statement="""
            INSERT INTO discovery_log (
                fund_id, url, discovered_from, method, priority_score,
                document_type, title, scope_decision, accepted,
                rejection_reason, is_document, run_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (fund_id, run_id, url) DO UPDATE SET
                discovered_from = excluded.discovered_from,
                method = excluded.method,
                priority_score = excluded.priority_score,
                document_type = excluded.document_type,
                title = excluded.title,
                scope_decision = excluded.scope_decision,
                accepted = excluded.accepted,
                rejection_reason = excluded.rejection_reason,
                is_document = excluded.is_document,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.fund_id,
            record.url,
            record.discovered_from,
            record.method,
            record.priority_score,
            record.document_type,
            record.title,
            record.scope_decision,
            int(record.accepted),
            record.rejection_reason,
            int(record.is_document),
            record.run_id,
            utc_now_iso(now),
        ),
        description="discovery log entry",
    )


def list_discovery_entries(
    path: Path,
    *,
    fund_id: str,
    run_id: str | None = None,
) -> list[DiscoveryLogRecord]:
    """Return what discovery recorded for one fund."""

    statement = """
        SELECT fund_id, url, discovered_from, method, priority_score,
               document_type, title, scope_decision, accepted,
               rejection_reason, is_document, run_id
        FROM discovery_log
        WHERE fund_id = ?
        """

    parameters: tuple[object, ...] = (fund_id,)

    if run_id is not None:
        statement += " AND run_id = ?"

        parameters = (
            fund_id,
            run_id,
        )

    statement += " ORDER BY priority_score DESC, url"

    return [
        DiscoveryLogRecord(
            fund_id=str(row[0]),
            url=str(row[1]),
            discovered_from=_optional_text(row[2]),
            method=str(row[3]),
            priority_score=int(row[4]),
            document_type=_optional_text(row[5]),
            title=_optional_text(row[6]),
            scope_decision=_optional_text(row[7]),
            accepted=bool(row[8]),
            rejection_reason=_optional_text(row[9]),
            is_document=bool(row[10]),
            run_id=str(row[11]),
        )
        for row in _select_rows(
            path,
            statement=statement,
            parameters=parameters,
            description="discovery log entries",
        )
    ]


@dataclass(frozen=True, slots=True)
class DocumentMetadataRecord:
    """What was learned about one parsed document before extraction."""

    source_id: int
    fund_id: str
    document_type: str | None = None
    type_score: int = 0
    type_ambiguous: bool = False
    type_evidence: str | None = None
    scope: str | None = None
    scope_confidence: str | None = None
    scope_accepted: bool = False
    scope_rejection_reason: str | None = None
    identity_evidence: str | None = None
    subfund_name: str | None = None
    share_class: str | None = None
    matched_ico: str | None = None
    matched_isin: str | None = None
    published_at: str | None = None
    published_at_origin: str | None = None
    effective_at: str | None = None
    reporting_period_start: str | None = None
    reporting_period_end: str | None = None
    as_of: str | None = None
    parser_name: str | None = None
    ocr_used: bool = False
    pages_parsed: int = 0
    parse_warnings: str | None = None


DOCUMENT_METADATA_COLUMNS = (
    "source_id, fund_id, document_type, type_score, type_ambiguous, "
    "type_evidence, scope, scope_confidence, scope_accepted, "
    "scope_rejection_reason, identity_evidence, subfund_name, share_class, "
    "matched_ico, matched_isin, published_at, published_at_origin, "
    "effective_at, reporting_period_start, reporting_period_end, as_of, "
    "parser_name, ocr_used, pages_parsed, parse_warnings"
)


def record_document_metadata(
    path: Path,
    record: DocumentMetadataRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Store what was learned about one document."""

    _execute_write(
        path,
        statement=f"""
            INSERT INTO document_metadata (
                {DOCUMENT_METADATA_COLUMNS}, updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT (source_id) DO UPDATE SET
                document_type = excluded.document_type,
                type_score = excluded.type_score,
                type_ambiguous = excluded.type_ambiguous,
                type_evidence = excluded.type_evidence,
                scope = excluded.scope,
                scope_confidence = excluded.scope_confidence,
                scope_accepted = excluded.scope_accepted,
                scope_rejection_reason = excluded.scope_rejection_reason,
                identity_evidence = excluded.identity_evidence,
                subfund_name = excluded.subfund_name,
                share_class = excluded.share_class,
                matched_ico = excluded.matched_ico,
                matched_isin = excluded.matched_isin,
                published_at = excluded.published_at,
                published_at_origin = excluded.published_at_origin,
                effective_at = excluded.effective_at,
                reporting_period_start = excluded.reporting_period_start,
                reporting_period_end = excluded.reporting_period_end,
                as_of = excluded.as_of,
                parser_name = excluded.parser_name,
                ocr_used = excluded.ocr_used,
                pages_parsed = excluded.pages_parsed,
                parse_warnings = excluded.parse_warnings,
                updated_at = excluded.updated_at
            """,
        parameters=(
            record.source_id,
            record.fund_id,
            record.document_type,
            record.type_score,
            int(record.type_ambiguous),
            record.type_evidence,
            record.scope,
            record.scope_confidence,
            int(record.scope_accepted),
            record.scope_rejection_reason,
            record.identity_evidence,
            record.subfund_name,
            record.share_class,
            record.matched_ico,
            record.matched_isin,
            record.published_at,
            record.published_at_origin,
            record.effective_at,
            record.reporting_period_start,
            record.reporting_period_end,
            record.as_of,
            record.parser_name,
            int(record.ocr_used),
            record.pages_parsed,
            record.parse_warnings,
            utc_now_iso(now),
        ),
        description="document metadata",
    )


def list_document_metadata(
    path: Path,
    *,
    fund_id: str,
) -> list[DocumentMetadataRecord]:
    """Return what is stored about the documents of one fund."""

    return [
        DocumentMetadataRecord(
            source_id=int(row[0]),
            fund_id=str(row[1]),
            document_type=_optional_text(row[2]),
            type_score=int(row[3]),
            type_ambiguous=bool(row[4]),
            type_evidence=_optional_text(row[5]),
            scope=_optional_text(row[6]),
            scope_confidence=_optional_text(row[7]),
            scope_accepted=bool(row[8]),
            scope_rejection_reason=_optional_text(row[9]),
            identity_evidence=_optional_text(row[10]),
            subfund_name=_optional_text(row[11]),
            share_class=_optional_text(row[12]),
            matched_ico=_optional_text(row[13]),
            matched_isin=_optional_text(row[14]),
            published_at=_optional_text(row[15]),
            published_at_origin=_optional_text(row[16]),
            effective_at=_optional_text(row[17]),
            reporting_period_start=_optional_text(row[18]),
            reporting_period_end=_optional_text(row[19]),
            as_of=_optional_text(row[20]),
            parser_name=_optional_text(row[21]),
            ocr_used=bool(row[22]),
            pages_parsed=int(row[23]),
            parse_warnings=_optional_text(row[24]),
        )
        for row in _select_rows(
            path,
            statement=f"""
                SELECT {DOCUMENT_METADATA_COLUMNS}
                FROM document_metadata
                WHERE fund_id = ?
                ORDER BY source_id
                """,
            parameters=(fund_id,),
            description="document metadata",
        )
    ]


def _execute_write(
    path: Path,
    *,
    statement: str,
    parameters: tuple[object, ...],
    description: str,
) -> None:
    try:
        with closing(connect_database(path)) as connection, connection:
            _ensure_initialized(
                connection,
                path,
            )

            connection.execute(
                statement,
                parameters,
            )
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not store {description} in {path}: {exc}") from exc


def _select_rows(
    path: Path,
    *,
    statement: str,
    parameters: tuple[object, ...],
    description: str,
) -> list[tuple[Any, ...]]:
    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            return [tuple(row) for row in connection.execute(statement, parameters).fetchall()]
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not read {description} from {path}: {exc}") from exc


def _optional_text(
    value: object,
) -> str | None:
    return str(value) if value is not None else None


def _key_text(
    value: str | None,
) -> str:
    """Return a uniqueness key component that is never NULL."""

    return value if value is not None else ""


def _optional_key(
    value: object,
) -> str | None:
    """Return a uniqueness key component as the absent value it means."""

    text = str(value) if value is not None else ""

    return text or None
