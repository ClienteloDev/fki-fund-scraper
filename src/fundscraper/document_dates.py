"""
The dates a fund document carries.

One document states several different dates and they mean different
things. A statute is effective from one date and was drawn up on another.
An annual report covers a period and was published after it ended. A
factsheet states the day its figures are valid for. Storing them all as
one "date" made a value from a five year old report look as current as
one from last month.

Five dates are read separately:

``published_at``            when the document was issued
``effective_at``            from when its content applies
``reporting_period_start``  the first day of the period it covers
``reporting_period_end``    the last day of that period
``as_of``                   the day its figures are valid for

Each is recorded with where it came from. A date read inside the document
is preferred over one guessed from the file name, and a date that could
only be taken from the file name says so, so that a reader can discount
it.

The module is pure and performs no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from fundscraper.document_classification import file_name_text
from fundscraper.html_discovery import normalize_search_text

MONTHS: Final[dict[str, int]] = {
    "leden": 1,
    "ledna": 1,
    "january": 1,
    "unor": 2,
    "unora": 2,
    "february": 2,
    "brezen": 3,
    "brezna": 3,
    "march": 3,
    "duben": 4,
    "dubna": 4,
    "april": 4,
    "kveten": 5,
    "kvetna": 5,
    "may": 5,
    "cerven": 6,
    "cervna": 6,
    "june": 6,
    "cervenec": 7,
    "cervence": 7,
    "july": 7,
    "srpen": 8,
    "srpna": 8,
    "august": 8,
    "zari": 9,
    "september": 9,
    "rijen": 10,
    "rijna": 10,
    "october": 10,
    "listopad": 11,
    "listopadu": 11,
    "november": 11,
    "prosinec": 12,
    "prosince": 12,
    "december": 12,
}


class DateOrigin(StrEnum):
    """Where a date was read from."""

    DOCUMENT_TEXT = "document_text"
    DOCUMENT_METADATA = "document_metadata"
    FILE_NAME = "file_name"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True)
class DatedValue:
    """One date together with the wording and the place it came from."""

    value: date
    origin: DateOrigin
    wording: str = ""
    confidence: str = "medium"


@dataclass(frozen=True, slots=True)
class DocumentDates:
    """Every date one document states."""

    published_at: DatedValue | None = None
    effective_at: DatedValue | None = None
    reporting_period_start: DatedValue | None = None
    reporting_period_end: DatedValue | None = None
    as_of: DatedValue | None = None


# Python forbids one group name appearing twice in a pattern, so every
# date shape carries its own names and they are resolved afterwards.
DAY_MONTH_YEAR: Final = (
    r"(?P<num_day>\d{1,2})\s*[./]\s*(?P<num_month>\d{1,2})\s*[./]\s*(?P<num_year>\d{4})"
)

ISO_DATE: Final = r"(?P<iso_year>\d{4})-(?P<iso_month>\d{2})-(?P<iso_day>\d{2})"

TEXT_DATE: Final = r"(?P<text_day>\d{1,2})\.?\s+(?P<month_name>[a-z]+)\s+(?P<text_year>\d{4})"


def _labelled(
    labels: str,
) -> re.Pattern[str]:
    """Build a pattern matching a date shortly after one of the labels."""

    return re.compile(
        rf"(?P<wording>{labels})[^0-9]{{0,24}}?"
        rf"(?:{DAY_MONTH_YEAR}|{ISO_DATE}|{TEXT_DATE})",
        re.VERBOSE | re.IGNORECASE,
    )


# Wording that introduces the day a document takes effect.
EFFECTIVE_PATTERN: Final = _labelled(
    r"ucinny\s+od|ucinnost\s+od|ucinnosti\s+od|nabyva\s+ucinnosti|platny\s+od"
    r"|plati\s+od|effective\s+from|effective\s+as\s+of|valid\s+from|in\s+effect\s+from"
)


# Wording that introduces the day a document was issued.
PUBLISHED_PATTERN: Final = _labelled(
    r"ze\s+dne|vyhotoveno\s+dne|vyhotoveno|datum\s+vyhotoveni|zverejneno\s+dne|zverejneno"
    r"|vydano\s+dne|v\s+praze\s+dne|published\s+on|published|date\s+of\s+issue|issued\s+on"
)


# Wording that introduces the day the figures are valid for.
AS_OF_PATTERN: Final = _labelled(
    r"k\s+datu|ke\s+dni|stav\s+k|udaje\s+k|hodnota\s+k|k\s+poslednimu\s+dni"
    r"|as\s+of|as\s+at|valuation\s+date"
)


# Wording that introduces the last day of a reporting period.
PERIOD_END_PATTERN: Final = _labelled(
    r"obdobi\s+koncici|za\s+obdobi\s+koncici|k\s+rozvahovemu\s+dni|rozvahovy\s+den"
    r"|for\s+the\s+year\s+ended|year\s+ended|period\s+ended|ended\s+on"
)


# A period written as a span: "za obdobi od 1.1.2024 do 31.12.2024".
PERIOD_RANGE_PATTERN: Final = re.compile(
    rf"(?P<wording>za\s+obdobi|obdobi|for\s+the\s+period)\s*(?:od|from)?\s*"
    rf"(?:{DAY_MONTH_YEAR}|{ISO_DATE})"
    rf"\s*(?:do|to|az\s+do|-|–)\s*"
    rf"(?P<end_day>\d{{1,2}})\s*[./]\s*(?P<end_month>\d{{1,2}})\s*[./]\s*(?P<end_year>\d{{4}})",
    re.IGNORECASE,
)


# A reporting year written without a day: "za rok 2024".
REPORTING_YEAR_PATTERN: Final = re.compile(
    r"(?P<wording>za\s+rok|za\s+ucetni\s+obdobi|for\s+the\s+year)\s*:?\s*(?P<year>20\d{2})",
    re.IGNORECASE,
)


# A four digit year in a file name. Used only when the document itself
# states nothing, and marked as such.
FILE_NAME_YEAR_PATTERN: Final = re.compile(r"(?<!\d)(?P<year>20\d{2})(?!\d)")


FILE_NAME_DATE_PATTERN: Final = re.compile(
    r"""
    (?<!\d)
    (?P<year>20\d{2})
    [-_ ]?
    (?P<month>0[1-9]|1[0-2])
    [-_ ]?
    (?P<day>0[1-9]|[12]\d|3[01])
    (?!\d)
    """,
    re.VERBOSE,
)


# How much of the document is read for its dates. They stand on the title
# page and in the closing signature block, not in the middle.
HEAD_CHARACTERS: Final = 6_000

TAIL_CHARACTERS: Final = 4_000


def extract_document_dates(
    *,
    text: str,
    url: str = "",
    metadata_text: str = "",
) -> DocumentDates:
    """
    Read every date one document states.

    The opening and the closing of the document are searched, because a
    statute names its effective date on the title page and its issue date
    under the signatures. The file name is consulted only for what the
    text does not say.
    """

    searchable = normalize_search_text(
        " ".join(
            (
                text[:HEAD_CHARACTERS],
                text[-TAIL_CHARACTERS:] if len(text) > HEAD_CHARACTERS else "",
                metadata_text,
            )
        )
    )

    effective = _first_labelled(
        pattern=EFFECTIVE_PATTERN,
        text=searchable,
    )

    published = _first_labelled(
        pattern=PUBLISHED_PATTERN,
        text=searchable,
    )

    as_of = _first_labelled(
        pattern=AS_OF_PATTERN,
        text=searchable,
    )

    period_start, period_end = _reporting_period(searchable)

    if period_end is None:
        period_end = _first_labelled(
            pattern=PERIOD_END_PATTERN,
            text=searchable,
        )

    if period_start is None and period_end is None:
        period_start, period_end = _reporting_year(searchable)

    if published is None:
        published = _from_file_name(url)

    return DocumentDates(
        published_at=published,
        effective_at=effective,
        reporting_period_start=period_start,
        reporting_period_end=period_end,
        as_of=as_of,
    )


def _first_labelled(
    *,
    pattern: re.Pattern[str],
    text: str,
) -> DatedValue | None:
    for match in pattern.finditer(text):
        parsed = _date_from_match(match)

        if parsed is not None:
            return DatedValue(
                value=parsed,
                origin=DateOrigin.DOCUMENT_TEXT,
                wording=" ".join(match.group("wording").split()),
                confidence="high",
            )

    return None


def _reporting_period(
    text: str,
) -> tuple[DatedValue | None, DatedValue | None]:
    match = PERIOD_RANGE_PATTERN.search(text)

    if match is None:
        return (
            None,
            None,
        )

    start = _date_from_match(match)

    end = _safe_date(
        year=int(match.group("end_year")),
        month=int(match.group("end_month")),
        day=int(match.group("end_day")),
    )

    wording = " ".join(match.group("wording").split())

    return (
        (
            DatedValue(
                value=start,
                origin=DateOrigin.DOCUMENT_TEXT,
                wording=wording,
                confidence="high",
            )
            if start is not None
            else None
        ),
        (
            DatedValue(
                value=end,
                origin=DateOrigin.DOCUMENT_TEXT,
                wording=wording,
                confidence="high",
            )
            if end is not None
            else None
        ),
    )


def _reporting_year(
    text: str,
) -> tuple[DatedValue | None, DatedValue | None]:
    """Turn "za rok 2024" into the calendar year it names."""

    match = REPORTING_YEAR_PATTERN.search(text)

    if match is None:
        return (
            None,
            None,
        )

    year = int(match.group("year"))

    wording = " ".join(match.group("wording").split())

    return (
        DatedValue(
            value=date(year, 1, 1),
            origin=DateOrigin.DERIVED,
            wording=wording,
            confidence="medium",
        ),
        DatedValue(
            value=date(year, 12, 31),
            origin=DateOrigin.DERIVED,
            wording=wording,
            confidence="medium",
        ),
    )


def _from_file_name(
    url: str,
) -> DatedValue | None:
    """
    Fall back to the date a file name carries.

    The origin says where it came from, because whoever uploaded the file
    chose that name and it may have nothing to do with the content.
    """

    if not url:
        return None

    name = file_name_text(url)

    match = FILE_NAME_DATE_PATTERN.search(name.replace(" ", ""))

    if match is not None:
        parsed = _safe_date(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day")),
        )

        if parsed is not None:
            return DatedValue(
                value=parsed,
                origin=DateOrigin.FILE_NAME,
                wording=name,
                confidence="low",
            )

    year_match = FILE_NAME_YEAR_PATTERN.search(name)

    if year_match is None:
        return None

    return DatedValue(
        value=date(int(year_match.group("year")), 1, 1),
        origin=DateOrigin.FILE_NAME,
        wording=name,
        confidence="low",
    )


def _date_from_match(
    match: re.Match[str],
) -> date | None:
    """Resolve whichever of the three date shapes actually matched."""

    groups = match.groupdict()

    if groups.get("num_year"):
        return _safe_date(
            year=int(groups["num_year"]),
            month=int(groups["num_month"]),
            day=int(groups["num_day"]),
        )

    if groups.get("iso_year"):
        return _safe_date(
            year=int(groups["iso_year"]),
            month=int(groups["iso_month"]),
            day=int(groups["iso_day"]),
        )

    month_name = groups.get("month_name")

    if groups.get("text_year") and month_name:
        month = MONTHS.get(month_name)

        if month is None:
            return None

        return _safe_date(
            year=int(groups["text_year"]),
            month=month,
            day=int(groups["text_day"]),
        )

    return None


def _safe_date(
    *,
    year: int,
    month: int,
    day: int,
) -> date | None:
    try:
        return date(
            year,
            month,
            day,
        )
    except ValueError:
        return None
