from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from fundscraper.models import FundInput
from fundscraper.normalization import canonical_url
from fundscraper.output_service import stable_fund_id

SCHEMA_VERSION: Final = 2


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
    document_type: str | None
    content_type: str | None
    document_format: str
    parser_name: str
    page_count: int
    character_count: int
    scanned_candidate: bool
    text_path: str
    parsed_at: str


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
            fund.web,
            canonical_url(fund.web),
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
    """Return parsed documents available for extraction."""

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
                    sources.document_type,
                    sources.content_type,
                    parsed.document_format,
                    parsed.parser_name,
                    parsed.page_count,
                    parsed.character_count,
                    parsed.scanned_candidate,
                    parsed.text_path,
                    parsed.parsed_at
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
            document_type=(str(row[3]) if row[3] is not None else None),
            content_type=(str(row[4]) if row[4] is not None else None),
            document_format=str(row[5]),
            parser_name=str(row[6]),
            page_count=int(row[7]),
            character_count=int(row[8]),
            scanned_candidate=bool(row[9]),
            text_path=str(row[10]),
            parsed_at=str(row[11]),
        )
        for row in rows
    ]


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
