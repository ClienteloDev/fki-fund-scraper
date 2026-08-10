"""
Reading values out of the tables of a document rather than its text.

A financial statement states its capital in a grid: the dates stand in
the header row, the labels in the first column, and the figures where the
two meet. Flattened into a paragraph that grid becomes a run of numbers
with no reliable way back to the label or the date they belonged to,
which is how a fund capital ended up dated by whichever date happened to
be nearest in the text.

This module reads the same grid as a grid. A value is only produced when
it can be tied to a label in its own row and, where the table has one, to
a date in its own column. Nothing here widens a search window; a value
that cannot be tied to both is simply not produced, and the existing text
extractor remains responsible for those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from fundscraper.document_parser import DocumentTable
from fundscraper.html_discovery import normalize_search_text

# A cell holding a date or a bare year, which is what turns a column into
# a period. "k datu" columns carry the first, yearly tables the second.
CELL_DATE_PATTERN: Final = re.compile(
    r"^\s*(?P<day>\d{1,2})\s*[./]\s*(?P<month>\d{1,2})\s*[./]\s*(?P<year>\d{4})\s*$"
)

CELL_ISO_PATTERN: Final = re.compile(r"^\s*(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\s*$")

CELL_YEAR_PATTERN: Final = re.compile(r"^\s*(?P<year>19\d{2}|20\d{2})\s*$")


# A number as a Czech document writes it: spaces between thousands, a
# comma before the decimals, and an optional sign.
CELL_NUMBER_PATTERN: Final = re.compile(
    r"^\s*(?P<sign>[-+−])?\s*(?P<digits>\d[\d  ]*(?:[.,]\d+)?)\s*(?P<unit>%|Kč|CZK|EUR|USD)?\s*$"
)


CURRENCY_IN_TEXT: Final[tuple[tuple[str, str], ...]] = (
    ("czk", "CZK"),
    ("kc", "CZK"),
    ("eur", "EUR"),
    ("usd", "USD"),
)


# How many leading rows may hold the header of a table. A statement puts
# the dates on the first row; some put a title above them.
MAXIMUM_HEADER_ROWS: Final = 3


# The longest label a row may carry. Beyond this the first cell is a
# paragraph that the table detector swallowed, not a label.
MAXIMUM_LABEL_CHARACTERS: Final = 120


@dataclass(frozen=True, slots=True)
class TableValue:
    """One figure, with the label and the period it was found under."""

    label: str
    value: float
    column: int
    as_of: date | None = None
    year: int | None = None
    currency: str | None = None
    is_percentage: bool = False
    row_text: str = ""


def column_periods(
    table: DocumentTable,
) -> tuple[dict[int, date], dict[int, int]]:
    """
    Return the dates and years the header columns of a table stand for.

    A statement writes "31.12.2024" and "31.12.2023" across the top and
    then one row per figure. Knowing which column is which period is what
    lets a figure keep its own date instead of borrowing a neighbour's.
    """

    dates: dict[int, date] = {}

    years: dict[int, int] = {}

    for row in table.rows[:MAXIMUM_HEADER_ROWS]:
        for index, cell in enumerate(row):
            if index in dates or index in years:
                continue

            parsed = _cell_date(cell)

            if parsed is not None:
                dates[index] = parsed

                continue

            year = _cell_year(cell)

            if year is not None:
                years[index] = year

    return (
        dates,
        years,
    )


def iter_table_values(
    table: DocumentTable,
) -> list[TableValue]:
    """
    Return every figure of a table together with its label and period.

    The label is the first cell of the row that carries text; the period
    comes from the header of the column the figure sits in. A row without
    a label, or a figure in a column with no header, still yields a value
    so that a two column "label / amount" table works, but it carries no
    period and the caller has to decide whether that is enough.
    """

    dates, years = column_periods(table)

    values: list[TableValue] = []

    for row in table.rows:
        label = _row_label(row)

        if not label:
            continue

        row_text = " ".join(cell for cell in row if cell)

        for index, cell in enumerate(row):
            if not cell or cell == label:
                continue

            number = _cell_number(cell)

            if number is None:
                continue

            amount, unit = number

            values.append(
                TableValue(
                    label=label,
                    value=amount,
                    column=index,
                    as_of=dates.get(index),
                    year=years.get(index),
                    currency=_currency_of(
                        unit=unit,
                        label=label,
                        row_text=row_text,
                    ),
                    is_percentage=(unit == "%"),
                    row_text=row_text,
                )
            )

    return values


def _row_label(
    row: tuple[str, ...],
) -> str:
    """
    Return the label of one row.

    A grid pads its cells, so the label is the first cell that carries
    words rather than the first cell outright.
    """

    for cell in row:
        cleaned = cell.strip()

        if not cleaned or len(cleaned) > MAXIMUM_LABEL_CHARACTERS:
            continue

        if _cell_number(cleaned) is not None:
            continue

        if _cell_date(cleaned) is not None or _cell_year(cleaned) is not None:
            continue

        if any(character.isalpha() for character in cleaned):
            return cleaned

    return ""


def _cell_number(
    cell: str,
) -> tuple[float, str | None] | None:
    """Return the figure a cell holds, with the unit written next to it."""

    match = CELL_NUMBER_PATTERN.match(cell)

    if match is None:
        return None

    digits = (
        match.group("digits")
        .replace(
            " ",
            "",
        )
        .replace(
            " ",
            "",
        )
    )

    if not digits or not digits[0].isdigit():
        return None

    # A Czech document separates decimals with a comma. A dot inside a
    # grouped number is a thousands separator, so it is dropped rather
    # than treated as a decimal point.
    if "," in digits:
        digits = digits.replace(
            ".",
            "",
        ).replace(
            ",",
            ".",
        )
    elif digits.count(".") == 1 and len(digits.split(".")[-1]) == 3:
        digits = digits.replace(
            ".",
            "",
        )

    try:
        amount = float(digits)
    except ValueError:
        return None

    if match.group("sign") in {"-", "−"}:
        amount = -amount

    return (
        amount,
        match.group("unit"),
    )


def _cell_date(
    cell: str,
) -> date | None:
    for pattern in (
        CELL_DATE_PATTERN,
        CELL_ISO_PATTERN,
    ):
        match = pattern.match(cell)

        if match is None:
            continue

        try:
            return date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            return None

    return None


def _cell_year(
    cell: str,
) -> int | None:
    match = CELL_YEAR_PATTERN.match(cell)

    if match is None:
        return None

    return int(match.group("year"))


def _currency_of(
    *,
    unit: str | None,
    label: str,
    row_text: str,
) -> str | None:
    """
    Return the currency a figure is stated in.

    A statement writes it once, in the label of the row - "Fondovy
    kapital Podfondu (Kc)" - rather than beside every number.
    """

    if unit and unit != "%":
        normalized = normalize_search_text(unit)

        for marker, currency in CURRENCY_IN_TEXT:
            if normalized == marker:
                return currency

    for text in (
        label,
        row_text,
    ):
        normalized = normalize_search_text(text)

        for marker, currency in CURRENCY_IN_TEXT:
            if re.search(
                rf"\b{marker}\b",
                normalized,
            ):
                return currency

    return None


# A percentage opening a cell. A fee table states the rate first and then
# the condition in the same cell: "30 % pri odkupu investicnich akcii".
LEADING_PERCENT_PATTERN: Final = re.compile(
    r"^\s*(?P<value>\d{1,3}(?:[.,]\d+)?)\s*%",
)


@dataclass(frozen=True, slots=True)
class TableRow:
    """One row of a table, kept as its label and its remaining cells."""

    label: str
    cells: tuple[str, ...]
    row_text: str


def iter_labelled_rows(
    table: DocumentTable,
) -> list[TableRow]:
    """
    Return the rows of a table that open with a label.

    Unlike :func:`iter_table_values` this keeps the cells as written, so
    a caller can read a figure that shares its cell with the condition
    attached to it without the value having to be the whole cell.
    """

    rows: list[TableRow] = []

    for row in table.rows:
        label = _row_label(row)

        if not label:
            continue

        rows.append(
            TableRow(
                label=label,
                cells=tuple(cell for cell in row if cell and cell != label),
                row_text=" ".join(cell for cell in row if cell),
            )
        )

    return rows


def leading_percentage(
    cell: str,
) -> float | None:
    """Return the percentage a cell opens with, if it opens with one."""

    match = LEADING_PERCENT_PATTERN.match(cell)

    if match is None:
        return None

    try:
        return float(
            match.group("value").replace(
                ",",
                ".",
            )
        )
    except ValueError:
        return None
