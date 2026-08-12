"""
Telling an expected miss apart from something that went wrong.

Discovery guesses. It asks every site for `/robots.txt` and for the three
conventional sitemap addresses, because a site that publishes one of them
hands over its whole document list for the price of a single request. Most
sites publish none of them, and the 404 that comes back is the guess being
answered, not a failure.

Reporting those answers as failures buried the real ones: a full run
produced 874 "failures", of which more than half were guessed sitemap
addresses and exactly one was a `MemoryError`. This module keeps the two
apart without hiding either.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from fundscraper.sitemap_discovery import DEFAULT_SITEMAP_PATHS, ROBOTS_PATH


class DiagnosticLevel(StrEnum):
    """How much attention one recorded outcome deserves."""

    EXPECTED_MISS = "expected_miss"
    WARNING = "warning"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One thing that did not go as hoped, and how much it matters."""

    level: DiagnosticLevel
    stage: str
    code: str
    message: str
    url: str | None = None

    def rendered(self) -> str:
        """Return the single-line form the reports have always carried."""

        if self.url:
            return f"{self.stage}: {self.url}: {self.code}: {self.message}"

        return f"{self.stage}: {self.code}: {self.message}"


# The addresses discovery guesses at rather than follows a link to. A
# site that does not publish them owes nobody an explanation.
GUESSED_PATHS: Final[frozenset[str]] = frozenset({ROBOTS_PATH, *DEFAULT_SITEMAP_PATHS})


# HTTP answers that mean "this address does not exist here". For a
# guessed address that is the expected answer.
ABSENT_STATUS_CODES: Final[frozenset[int]] = frozenset({403, 404, 410})


# Error codes that describe a document rather than the run. A single one
# costs one source; it does not mean the crawler is broken.
DOCUMENT_LEVEL_CODES: Final[frozenset[str]] = frozenset(
    {
        "http_status_error",
        "network_error",
        "invalid_url",
        "response_too_large",
        "document_parse_error",
        "unsupported_document_format",
        "scanned_document_ocr_failed",
    }
)


def is_guessed_address(
    url: str,
) -> bool:
    """Return whether discovery reached this address by guessing it."""

    path = urlsplit(url).path.casefold()

    return path in GUESSED_PATHS


def classify_fetch_failure(
    *,
    url: str,
    code: str,
    status_code: int | None = None,
    guessed: bool | None = None,
) -> DiagnosticLevel:
    """
    Rank one failed fetch.

    ``guessed`` overrides the address check, so a caller that already
    knows it invented the address does not have to be second-guessed by
    a pattern.
    """

    invented = is_guessed_address(url) if guessed is None else guessed

    if invented and (status_code in ABSENT_STATUS_CODES or code == "http_status_error"):
        return DiagnosticLevel.EXPECTED_MISS

    if code in DOCUMENT_LEVEL_CODES:
        return DiagnosticLevel.WARNING

    return DiagnosticLevel.FAILURE


# How many server errors from one host stop being bad luck. A site that
# answers 5xx this many times is not serving its documents at all.
REPEATED_SERVER_ERROR_LIMIT: Final = 3


def escalate_repeated_server_errors(
    diagnostics: list[Diagnostic],
) -> list[Diagnostic]:
    """
    Raise repeated server errors from one host to a failure.

    One 5xx is a bad moment. Several from the same host across one fund
    means its documents were not served, and a run that reports that as a
    warning tells a reader the fund simply has nothing.
    """

    by_host: Counter[str] = Counter(
        _host(item.url)
        for item in diagnostics
        if item.url and item.code == "http_status_error" and " 5" in item.message
    )

    repeated = {host for host, count in by_host.items() if count >= REPEATED_SERVER_ERROR_LIMIT}

    if not repeated:
        return diagnostics

    return [
        (
            Diagnostic(
                level=DiagnosticLevel.FAILURE,
                stage=item.stage,
                code=item.code,
                message=(f"{item.message} (repeated server errors from {_host(item.url)})"),
                url=item.url,
            )
            if item.url and _host(item.url) in repeated and " 5" in item.message
            else item
        )
        for item in diagnostics
    ]


def _host(
    url: str | None,
) -> str:
    return urlsplit(url or "").netloc.casefold()


def summarize(
    diagnostics: list[Diagnostic],
) -> dict[str, int]:
    """Return how many of each level a run produced."""

    counted: Counter[str] = Counter(item.level.value for item in diagnostics)

    return {level.value: counted.get(level.value, 0) for level in DiagnosticLevel}
