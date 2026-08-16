"""
Choosing which parsed table an AI completion candidate should read.

A PDF parser emits `tables[]` for anything that looks like a grid, and a
two-column page of prose looks like a grid. The first table whose text
matches a keyword is therefore usually the wrong one: one fund's real
indicator table, carrying current and previous accounting-period columns,
sat on page 10 behind a paragraph the parser had shredded into cells on
page 3.

Nothing here extracts a value. It ranks the tables of one document so the
best-supported one is read, and refuses to call a candidate complete
unless its dates and units belong to the target row's own table.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Final

Row = list[Any]


def fold(value: Any) -> str:
    """Return lower-case text without diacritics."""

    decomposed = unicodedata.normalize("NFKD", str(value or ""))

    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


DATE_PATTERN: Final = re.compile(r"\d{1,2}\s*[./]\s*\d{1,2}\s*[./]\s*(?:19|20)\d{2}")

# The negative lookbehind keeps a Czech decimal fraction from reading as a
# year. A chart axis labelled "2,2000 Kč" was scored as a header carrying
# dates, because "2000" sits inside it. Dates never use a decimal comma,
# so refusing a comma-with-digit prefix costs nothing and "31.12.2025"
# still matches.
YEAR_PATTERN: Final = re.compile(r"(?<!\d,)\b(?:19|20)\d{2}\b")

UNIT_PATTERN: Final = re.compile(r"tis\.|mil\.|mld\.|\bk[cč]\b|\bczk\b|\beur\b|\busd\b|%")

PERCENT_PATTERN: Final = re.compile(r"%")

CURRENCY_PATTERN: Final = re.compile(r"\bk[cč]\b|\bczk\b|\beur\b|\busd\b")

NUMBER_PATTERN: Final = re.compile(r"\d")

# A run of two or more letters, so that stray glyphs the parser leaves
# behind ("e 1,0331 Kč") are not counted as words.
WORD_PATTERN: Final = re.compile(r"[^\W\d_]{2,}")


# Wording that tells a reader which period a column holds. This is the
# signal that matters most: one real table names the previous period
# before the current one, so any builder that reads columns by position
# inverts it.
PERIOD_WORDS: Final[tuple[str, ...]] = (
    "ucetniho obdobi",
    "predchazejiciho",
    "bezne ucetni",
    "minule ucetni",
    "stav k",
    "k datu",
    "zustatek k",
    "obdobi koncici",
)


FIELD_SIGNALS: Final[dict[str, tuple[str, ...]]] = {
    "aum_history": ("aktiva celkem", "cista aktiva", "fondovy kapital", "nav fondu", "zustatek k"),
    "assets_under_management": (
        "aktiva celkem",
        "cista aktiva",
        "fondovy kapital",
        "nav fondu",
    ),
    "historical_values": (
        "hodnota investicni akcie",
        "hodnota akcie",
        "hodnota prioritni akcie",
        # The wrapped labels, which carry the class but not yet the noun.
        "hodnota vykonnost",
        "hodnota prioritn",
        "hodnota rustov",
        "hodnota premiov",
        "cista aktiva na investicni akcii",
        "nav na akcii",
    ),
    "annual_returns": ("vykonnost", "zhodnoceni", "vynos fondu", "vynos podfondu"),
    "fees": (
        "vstupni poplatek",
        "vystupni poplatek",
        "naklady na vstup",
        "naklady na vystup",
        "poplatek za obhospodarovani",
        "vykonnostni poplatek",
        "celkova nakladovost",
    ),
}


# Which rows are worth reading for which field. These live here, next to
# FIELD_SIGNALS, so that "is this row evidence for this field" is decided
# in one place and can be tested.
FIELD_PROBES: Final[dict[str, re.Pattern[str]]] = {
    # "Hodnota <trida> investicni akcie" - the class name sits between the
    # two words, so the gap has to be allowed for. Without it the whole
    # family of "Hodnota Vykonnostni/Prioritni/Rustove investicni akcie"
    # rows was invisible to this field.
    #
    # The second alternative is for the same label cut short: these
    # reports wrap it, leaving "Hodnota vykonnostni" in the row and
    # "investicni akcie (VIA)" on the next one. Only the class adjectives
    # are listed, so that "hodnota investicniho portfolia" is not swept in.
    "historical_values": re.compile(
        r"hodnota\s+(?:\w+\s+){0,2}akcie"
        r"|hodnota\s+(?:vykonnost|prioritn|rustov|premiov)\w*"
        r"|hodnota podiloveho listu|nav na akcii|cista aktiva na investicni akcii"
    ),
    "fees": re.compile(
        r"vstupni poplatek|vystupni poplatek|naklady na vstup|naklady na vystup|"
        r"poplatek za obhospodarovani|vykonnostni poplatek|vykonnostni odmena|celkova nakladovost"
    ),
    "annual_returns": re.compile(r"vykonnost|zhodnoceni|vynos fondu|vynos podfondu"),
    "aum_history": re.compile(
        r"fondovy kapital|fondoveho kapitalu|cista aktiva|nav fondu|zustatek k"
    ),
    "assets_under_management": re.compile(
        r"fondovy kapital|fondoveho kapitalu|cista aktiva|nav fondu|aktiva celkem"
    ),
}


# "Vykonnostni investicni akcie" is the name of a share class, not a
# statement of performance, and "vykonnostni odmena" is a fee. Czech
# separates them by part of speech: the return is the noun "vykonnost",
# the class and the fee take the adjective "vykonnostni". So a probe hit
# that survives only on the adjective is not a return, while "vykonnost
# investicnich akcii za rok" - a real statement about the shares - is
# left alone.
PERFORMANCE_ADJECTIVE: Final = re.compile(r"vykonnostn\w*")

# The adjective rule alone is not enough, because these reports split the
# class name across rows: the target row reads "Hodnota vykonnostni" and
# the word "akcie" lands on the next one. One report also misspells the
# adjective as "Vykonnosti". Both are caught by what the row is: a row
# labelled "Hodnota ..." states a value, never a return.
VALUE_ROW_LABEL: Final = re.compile(r"^\s*hodnota\b")


# "Zustatek k <date>" opens the movement schedule of anything a fund
# reports: its capital, but equally its tax provisions, its repair
# reserves and its receivables. The row alone cannot tell them apart, so
# the table has to say what it is a movement *of*.
BALANCE_ROW_WORDING: Final = re.compile(r"zustatek k")

# What a movement schedule must be about for its balance to be the
# fund's. Matched against the whole table, because the subject is named
# in the column headings or in the rows above the balance line.
CAPITAL_SUBJECT_WORDING: Final = re.compile(r"kapital|cist\w* aktiv|nav fondu|\baktiva\b")


def balance_row_has_capital_subject(*, rows: list[Row], text: str) -> bool:
    """
    Return whether a "Zustatek k ..." row belongs to a capital schedule.

    A match that survives without the balance wording stands on its own -
    "Fondovy kapital ... zustatek k" needs no help. A match that lives
    only on the balance wording has to be vouched for by the table, which
    is what separates an equity movement from a tax or provisions note.
    """

    if not BALANCE_ROW_WORDING.search(text):
        return True

    remainder = BALANCE_ROW_WORDING.sub(" ", text)

    if FIELD_PROBES["aum_history"].search(remainder):
        return True

    whole = fold(" ".join(str(cell) for row in rows for cell in row))

    return bool(CAPITAL_SUBJECT_WORDING.search(whole))


def is_probe_match(*, field: str, text: str, probe: re.Pattern[str]) -> bool:
    """
    Return whether a probe hit on this row is really evidence for the field.

    A hit that survives only because of a share-class name does not count,
    but a row that names the class *and* states a return still does - the
    class wording is removed and the probe re-run on what is left.
    """

    if not probe.search(text):
        return False

    if field != "annual_returns":
        return True

    if VALUE_ROW_LABEL.match(text):
        return False

    return bool(probe.search(PERFORMANCE_ADJECTIVE.sub(" ", text)))


# A cell longer than this is a sentence, not a data point.
PROSE_CELL_LENGTH: Final = 60

# Below this score a table is not worth reading at all.
MINIMUM_TABLE_SCORE: Final = 4

# A cell holding this many words, and beginning mid-sentence, is the tail
# of a paragraph rather than a label or a value.
FRAGMENT_WORD_COUNT: Final = 3

# What kind of unit makes a value of this field readable at all. A rate
# is meaningless without a per-cent sign; an amount is meaningless
# without a currency, and a per-cent sign does not supply one.
CURRENCY_UNIT_FIELDS: Final[frozenset[str]] = frozenset(
    {"aum_history", "assets_under_management", "historical_values"}
)

PERCENT_UNIT_FIELDS: Final[frozenset[str]] = frozenset({"annual_returns"})


@dataclass(frozen=True, slots=True)
class TableAssessment:
    """What one candidate table is worth, and why."""

    score: int
    target_index: int | None
    reasons: tuple[str, ...] = dataclass_field(default_factory=tuple)
    is_prose: bool = False
    # Set when the probe matched a column heading rather than a data row.
    # `header_index` is the row that matched and `column_indexes` names the
    # heading cells it matched, so a reader knows which column of the
    # target row the probe was actually asking about.
    header_index: int | None = None
    column_indexes: tuple[int, ...] = dataclass_field(default_factory=tuple)


def _cells(rows: list[Row]) -> list[str]:
    return [str(c) for row in rows for c in row if str(c).strip()]


def looks_like_prose(rows: list[Row]) -> bool:
    """
    Return whether a parsed table is really a shredded paragraph.

    Two shapes give it away: cells that hold whole sentences, and rows
    whose width swings wildly because the parser cut a line wherever the
    glyphs happened to fall.
    """

    cells = _cells(rows)

    if not cells:
        return True

    average = sum(len(c) for c in cells) / len(cells)

    if average > PROSE_CELL_LENGTH:
        return True

    numeric = sum(1 for c in cells if NUMBER_PATTERN.search(c))

    return numeric == 0


def looks_like_sentence_fragment(row: Row) -> bool:
    """
    Return whether a row is the middle of a sentence cut into cells.

    The giveaway is a cell that starts mid-sentence and still carries
    several words. One annual report's chart caption reached the pool as
    a header "Hodn" above a row "ota Vykonnostni investicni akcie k 31.
    12. 2" / "025 cinila 2,6605 Kc." - a sentence, scored as a data row.

    A single stray glyph in front of a value ("e 1,0331 Kc") is left
    alone: it carries one word, not several.
    """

    for cell in row:
        text = str(cell).strip()

        if not text:
            continue

        first = text[0]

        if not first.isalpha() or not first.islower():
            continue

        if len(WORD_PATTERN.findall(text)) >= FRAGMENT_WORD_COUNT:
            return True

    return False


def is_header_row(row: Row) -> bool:
    """Return whether a row names columns rather than holding values."""

    filled = [str(c) for c in row if str(c).strip()]

    if not filled:
        return False

    # A sentence is not a header, however many dates it happens to
    # contain. One document's narrative row carried "V roce 2024 fond
    # pokracoval..." and was read as a dated column header.
    if any(len(c) > PROSE_CELL_LENGTH for c in filled):
        return False

    if looks_like_sentence_fragment(row):
        return False

    if any(any(word in fold(c) for word in PERIOD_WORDS) for c in filled):
        return True

    # Below this, the row has to name more than one column to be a
    # header at all. A row holding a single cell names nothing across
    # the table, and both remaining rules used to accept one: a lone
    # "Hodn" passed the no-numbers rule, and a lone "2,2000 Kc" passed
    # the dated rule vacuously, because zero dates cleared a negative
    # threshold.
    if len(filled) < 2:
        return False

    dated = sum(1 for c in filled if DATE_PATTERN.search(c) or YEAR_PATTERN.search(c))

    if dated and dated >= len(filled) - 2:
        return True

    return not any(NUMBER_PATTERN.search(c) for c in filled)


def unit_is_sufficient(*, field: str, text: str) -> bool:
    """
    Return whether the units present can carry this field's value.

    A per-cent sign is a unit, but not one an amount can be read in: a
    fund capital table whose only unit was the "Zmena v %" column read
    as complete, leaving the currency of the figure unknown.
    """

    has_percent = bool(PERCENT_PATTERN.search(text))
    has_currency = bool(CURRENCY_PATTERN.search(text))

    if field in CURRENCY_UNIT_FIELDS:
        return has_currency

    if field in PERCENT_UNIT_FIELDS:
        return has_percent

    # A fee is stated either as a rate or as a fixed amount.
    return has_percent or has_currency


def _first_data_row(rows: list[Row], after: int) -> int | None:
    """Index of the first row below `after` that carries figures."""

    for index in range(after + 1, len(rows)):
        if any(NUMBER_PATTERN.search(str(c)) for c in rows[index]):
            return index

    return None


def heading_match(
    *,
    rows: list[Row],
    index: int,
    field: str,
    probe: re.Pattern[str],
) -> tuple[int, tuple[int, ...]] | None:
    """
    Return the data row a heading match is really asking about.

    A probe can land on the column heading rather than on a value:

        ["Datum", "Hodnota investicni akcie", "Objem fondoveho kapitalu"]
        ["31.12.2025", "1,7661", "306 779 988 CZK"]

    Taking the heading as the target leaves no header above it, so the
    candidate used to be refused for want of the very row it had matched.
    The values live underneath, and the heading is what names their
    columns - so the target moves down and the heading becomes context.

    Returns the data row index and the heading cells that matched, or
    nothing when the matched row is an ordinary data row.
    """

    row = rows[index]

    if any(NUMBER_PATTERN.search(str(c)) for c in row):
        return None

    if not is_header_row(row):
        return None

    data_index = _first_data_row(rows, index)

    if data_index is None:
        return None

    columns = tuple(
        position
        for position, cell in enumerate(row)
        if str(cell).strip() and is_probe_match(field=field, text=fold(str(cell)), probe=probe)
    )

    return data_index, columns


def assess_table(
    *,
    rows: list[Row],
    field: str,
    probe: re.Pattern[str],
) -> TableAssessment:
    """Score one parsed table for one field, and locate the target row."""

    if not rows:
        return TableAssessment(score=0, target_index=None, reasons=("empty",))

    target_index = None

    for index, row in enumerate(rows):
        if is_probe_match(
            field=field,
            text=fold(" ".join(str(c) for c in row)),
            probe=probe,
        ):
            target_index = index
            break

    if target_index is None:
        return TableAssessment(score=0, target_index=None, reasons=("no target row",))

    if field == "aum_history" and not balance_row_has_capital_subject(
        rows=rows,
        text=fold(" ".join(str(c) for c in rows[target_index])),
    ):
        return TableAssessment(
            score=0,
            target_index=None,
            reasons=("balance row in a schedule that is not about fund capital",),
        )

    heading_index: int | None = None
    columns: tuple[int, ...] = ()

    moved = heading_match(rows=rows, index=target_index, field=field, probe=probe)

    if moved is not None:
        heading_index, columns = target_index, moved[1]
        target_index = moved[0]

    reasons: list[str] = []
    score = 0

    if looks_like_prose(rows):
        # A shredded paragraph must never outrank a real grid, however
        # many keywords it happens to contain.
        return TableAssessment(
            score=-10,
            target_index=target_index,
            reasons=("prose pseudo-table",),
            is_prose=True,
        )

    if looks_like_sentence_fragment(rows[target_index]):
        # The rest of the table may well be a grid; this row is not part
        # of it, so there is no value here to attribute.
        return TableAssessment(
            score=-10,
            target_index=target_index,
            reasons=("target row is a sentence fragment",),
            is_prose=True,
        )

    widths = [len([c for c in row if str(c).strip()]) for row in rows]
    data_rows = [row for row in rows if any(NUMBER_PATTERN.search(str(c)) for c in row)]

    if len(data_rows) >= 2:
        score += 3
        reasons.append("two or more data rows")

    if widths and max(widths) - min(widths) <= 2:
        score += 2
        reasons.append("stable row width")

    cells = _cells(rows)
    short = sum(1 for c in cells if len(c) <= 24)

    if cells and short / len(cells) >= 0.6:
        score += 2
        reasons.append("mostly short cells")

    header_rows = [row for row in rows[:target_index] if is_header_row(row)]

    if header_rows:
        score += 3
        reasons.append("header row above target")

    header_text = " ".join(str(c) for row in header_rows for c in row)

    if any(word in fold(header_text) for word in PERIOD_WORDS):
        score += 4
        reasons.append("header names the accounting periods")

    if DATE_PATTERN.search(header_text) or YEAR_PATTERN.search(header_text):
        score += 3
        reasons.append("header carries dates")

    target_text = " ".join(str(c) for c in rows[target_index])

    if UNIT_PATTERN.search(fold(target_text)) or UNIT_PATTERN.search(fold(header_text)):
        score += 2
        reasons.append("unit in target row or header")

    hits = sum(1 for signal in FIELD_SIGNALS.get(field, ()) if signal in fold(target_text))

    if hits:
        score += min(hits, 2)
        reasons.append(f"{hits} field signal(s) in target row")
    else:
        score -= 2
        reasons.append("field signal only elsewhere in table")

    if heading_index is not None:
        reasons.append("probe matched a column heading; target moved to the first data row")

    return TableAssessment(
        score=score,
        target_index=target_index,
        reasons=tuple(reasons),
        header_index=heading_index,
        column_indexes=columns,
    )


def select_best_table(
    *,
    tables: list[list[Row]],
    field: str,
    probe: re.Pattern[str],
) -> tuple[int, TableAssessment] | None:
    """
    Return the best-supported table of a document, or nothing.

    Ties keep the earlier table, which only matters when two are equally
    well supported.
    """

    best: tuple[int, TableAssessment] | None = None

    for index, rows in enumerate(tables):
        assessment = assess_table(rows=rows, field=field, probe=probe)

        if assessment.target_index is None:
            continue

        if assessment.score < MINIMUM_TABLE_SCORE:
            continue

        if best is None or assessment.score > best[1].score:
            best = (index, assessment)

    return best


def context_is_complete(
    *,
    rows: list[Row],
    assessment: TableAssessment,
    field: str,
) -> bool:
    """
    Return whether dates and units belong to the target row's own table.

    A stray year in a footnote and a stray currency three rows away are
    not context. The header has to carry the period, and the unit has to
    stand in the header or in the target row itself, and be a unit the
    field's own value can be read in - see `unit_is_sufficient`.
    """

    if assessment.is_prose or assessment.target_index is None:
        return False

    target_text = fold(" ".join(str(c) for c in rows[assessment.target_index]))

    if field == "fees":
        # A fee schedule states a rate, not a period, and it states it in
        # the row itself - "Vystupni poplatek (srazka) | 5 % z objemu
        # odkupovanych Investicnich akcii". No column header is needed to
        # read that, and requiring one only rewarded whatever text the
        # parser happened to leave above it.
        return unit_is_sufficient(field=field, text=target_text)

    header_rows = [row for row in rows[: assessment.target_index] if is_header_row(row)]

    if not header_rows:
        return False

    header_text = fold(" ".join(str(c) for row in header_rows for c in row))

    # A row can date itself: "Zustatek k 31.12.2025" or a date column
    # carries the period in the row's own label, not in the heading above
    # it. That is stated, not inferred, so it counts.
    dated_text = f"{header_text} {target_text}"

    has_period = (
        any(word in dated_text for word in PERIOD_WORDS)
        or bool(DATE_PATTERN.search(dated_text))
        or bool(YEAR_PATTERN.search(dated_text))
    )

    has_unit = unit_is_sufficient(field=field, text=dated_text)

    return has_period and has_unit
