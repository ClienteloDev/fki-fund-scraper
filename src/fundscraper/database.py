from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fundscraper.models import FundInput
from fundscraper.normalization import canonical_url
from fundscraper.output_service import stable_fund_id

SCHEMA_VERSION: Final = 1


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
"""


class DatabaseError(RuntimeError):
    """Raised when the processing database operation fails."""


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
    """Insert or update input funds in the processing database."""

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

    try:
        with closing(connect_database(path)) as connection:
            _ensure_initialized(
                connection,
                path,
            )

            with connection:
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
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not register funds in {path}: {exc}") from exc

    return len(rows)


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
