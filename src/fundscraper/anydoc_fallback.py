"""
A second reading of the PDFs whose layout defeated the Step 6 parser.

The parser hierarchy is unchanged: every document is still read by
:func:`fundscraper.document_parser.parse_document` first, and that result
is what the pipeline uses. This module only decides whether a particular
PDF is worth converting a second time through AnyDoc, and whether the
second reading was actually better.

Three rules keep the fallback from doing harm.

Some documents are never handed over, however bad their layout looks. A
key information document, a financial statement, and any parse that
already produced fee rows or a per-year series are the shapes where the
grid extraction of the primary parser wins outright, and where AnyDoc's
merged cells were measured handing one rate to several fees.

A replacement has to improve the structure without costing evidence. A
conversion that reads fewer words, or that loses the dates, years,
percentages or identifiers the primary parse found, is rejected even when
its tables look better, because a value the pipeline cannot attribute is
worth less than one it can.

The page a value was quoted from is preserved. AnyDoc reports no page
boundaries, so the conversion is matched back onto the pages the primary
parser already read, and anything that cannot be placed keeps no page
number at all - which lowers the confidence of any value quoted from it.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from fundscraper.anydoc_parser import (
    ANYDOC_PARSER_NAME,
    AnyDocParseError,
    MarkdownChunk,
    anydoc_available,
    anydoc_supports,
    convert_to_markdown,
    markdown_chunks,
)
from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentFormat,
    DocumentTable,
    ParsedDocument,
    ParsedPage,
    normalize_extracted_text,
)
from fundscraper.field_extraction import foreign_section_ratio
from fundscraper.html_discovery import normalize_search_text
from fundscraper.table_extraction import iter_labelled_rows, leading_percentage

# The name a fallback parse is recorded under. It keeps the leading word
# of the parser it came from, the way the PDF parser names the rung it
# answered from, so a reader of the database can tell at a glance which
# documents were read the second way.
ANYDOC_FALLBACK_PARSER_NAME: Final = f"{ANYDOC_PARSER_NAME}+layout_fallback"


class LayoutTrigger(StrEnum):
    """Why a document was considered for a second reading."""

    PARSER_LADDER_FALLBACK = "parser_ladder_fallback"
    BROKEN_READING_ORDER = "broken_reading_order"
    TEXT_WITHOUT_TABLES = "text_without_tables"
    MULTI_COLUMN_LAYOUT = "multi_column_layout"


class FallbackOutcome(StrEnum):
    """What became of the second reading."""

    NOT_TRIGGERED = "not_triggered"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


# ---------------------------------------------------------------------------
# Measuring one parse
# ---------------------------------------------------------------------------


def fold(value: str) -> str:
    """Strip diacritics and case so Czech wording matches one way."""

    decomposed = unicodedata.normalize("NFKD", value)

    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


WORD_PATTERN: Final = re.compile(r"[A-Za-zÀ-ž]{2,}")

# A sentence that ends and is followed by a lower-case word did not really
# end: the words after it belong somewhere else. Two columns read as one
# produce exactly this, because the end of the left column lands in front
# of the middle of the right one.
SENTENCE_END_LOWER_PATTERN: Final = re.compile(r"[.!?]\s+[a-záčďéěíňóřšťúůýž]")

SENTENCE_END_UPPER_PATTERN: Final = re.compile(r"[.!?]\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9]")

YEAR_PATTERN: Final = re.compile(r"\b(?:19[89]\d|20[0-4]\d)\b")

DATE_PATTERN: Final = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*((?:19|20)\d{2})\b")

ISO_DATE_PATTERN: Final = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")

ISIN_PATTERN: Final = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")

PERCENT_PATTERN: Final = re.compile(r"(-?\d{1,3}(?:[  ]?\d{3})*(?:[.,]\d+)?)\s*%")


FEE_ROW_KEYWORDS: Final[tuple[str, ...]] = (
    "poplatek",
    "poplatky",
    "uplata",
    "manazersk",
    "obhospodarov",
    "vstupni",
    "vystupni",
    "vykonnostni",
    "naklady na vstup",
    "naklady na vystup",
    "prubezne naklady",
)


def broken_sentence_ratio(
    text: str,
) -> float:
    """
    Return how many sentence ends are followed by a word that continues.

    The absolute value differs between documents, so it only says
    something when the same document is measured both ways.
    """

    broken = len(SENTENCE_END_LOWER_PATTERN.findall(text))

    whole = len(SENTENCE_END_UPPER_PATTERN.findall(text))

    if not (broken + whole):
        return 0.0

    return broken / (broken + whole)


@dataclass(frozen=True, slots=True)
class EvidenceTokens:
    """The tokens a value is dated, quoted and attributed by."""

    years: frozenset[str]
    dates: frozenset[str]
    percentages: frozenset[str]
    isins: frozenset[str]


def evidence_tokens(
    text: str,
) -> EvidenceTokens:
    """Read every token a later stage needs out of one parse."""

    dates = {
        f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        for day, month, year in DATE_PATTERN.findall(text)
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31
    }

    dates.update(
        f"{year}-{month}-{day}"
        for year, month, day in ISO_DATE_PATTERN.findall(text)
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31
    )

    return EvidenceTokens(
        years=frozenset(YEAR_PATTERN.findall(text)),
        dates=frozenset(dates),
        percentages=frozenset(
            value.replace(" ", " ").replace(" ", "").replace(",", ".")
            for value in PERCENT_PATTERN.findall(text)
        ),
        isins=frozenset(ISIN_PATTERN.findall(text)),
    )


def retention(
    *,
    primary: frozenset[str],
    candidate: frozenset[str],
) -> float:
    """Return how much of what the primary parse found survived."""

    if not primary:
        return 1.0

    return len(primary & candidate) / len(primary)


@dataclass(frozen=True, slots=True)
class LayoutQuality:
    """What one parse of a document is worth to the reader after it."""

    word_count: int
    character_count: int
    broken_sentence_ratio: float
    table_count: int
    labelled_rows: int
    fee_rows: int
    year_value_rows: int
    multi_column_pages: int
    page_count: int
    pages_with_numbers: int
    tokens: EvidenceTokens

    # How much of the document reads as belonging to some other fund. A
    # value found inside such a span is refused by the scope rules, so a
    # reading that raises this has taken attribution away whatever else
    # it improved. It stays zero when the fund is not known here.
    foreign_section_ratio: float = 0.0

    @property
    def multi_column_ratio(self) -> float:
        if not self.page_count:
            return 0.0

        return self.multi_column_pages / self.page_count

    def to_json_dict(self) -> dict[str, object]:
        return {
            "word_count": self.word_count,
            "character_count": self.character_count,
            "broken_sentence_ratio": round(self.broken_sentence_ratio, 4),
            "table_count": self.table_count,
            "labelled_rows": self.labelled_rows,
            "fee_rows": self.fee_rows,
            "year_value_rows": self.year_value_rows,
            "multi_column_pages": self.multi_column_pages,
            "page_count": self.page_count,
            "pages_with_numbers": self.pages_with_numbers,
            "foreign_section_ratio": round(self.foreign_section_ratio, 4),
            "distinct_years": len(self.tokens.years),
            "distinct_dates": len(self.tokens.dates),
            "distinct_percentages": len(self.tokens.percentages),
            "distinct_isins": len(self.tokens.isins),
        }


def _table_row_counts(
    tables: list[DocumentTable],
) -> tuple[int, int, int]:
    """Return the labelled, fee-bearing and per-year rows of some tables."""

    labelled = 0

    fee_rows = 0

    year_rows = 0

    for table in tables:
        for row in iter_labelled_rows(table):
            if not row.cells:
                continue

            labelled += 1

            normalized_label = normalize_search_text(row.label)

            has_rate = any(leading_percentage(cell) is not None for cell in row.cells)

            if has_rate and any(keyword in normalized_label for keyword in FEE_ROW_KEYWORDS):
                fee_rows += 1

            if YEAR_PATTERN.search(row.row_text):
                year_rows += 1

    return (
        labelled,
        fee_rows,
        year_rows,
    )


# How far apart two blocks must sit before the page counts as typeset in
# columns. A page is split down the middle and needs enough blocks wholly
# on each side for the split to mean anything.
MINIMUM_BLOCKS_PER_COLUMN: Final = 4

MINIMUM_BLOCKS_FOR_COLUMNS: Final = 8


def _multi_column_pages(
    document: ParsedDocument,
) -> int:
    """Return how many pages were typeset in more than one column."""

    total = 0

    for page in document.pages:
        blocks = page.blocks

        if len(blocks) < MINIMUM_BLOCKS_FOR_COLUMNS:
            continue

        left_edge = min(block.x0 for block in blocks)

        right_edge = max(block.x1 for block in blocks)

        if right_edge <= left_edge:
            continue

        middle = left_edge + (right_edge - left_edge) / 2

        left = sum(1 for block in blocks if block.x1 < middle)

        right = sum(1 for block in blocks if block.x0 > middle)

        if left >= MINIMUM_BLOCKS_PER_COLUMN and right >= MINIMUM_BLOCKS_PER_COLUMN:
            total += 1

    return total


def measure_layout(
    document: ParsedDocument,
    *,
    fund_name: str | None = None,
) -> LayoutQuality:
    """
    Measure one parse the way the fallback decision reads it.

    ``fund_name`` enables the attribution measurement. Without it the
    reading is still measured for structure and evidence, which is all a
    trigger needs.
    """

    text = document.full_text

    tables = [table for _, table in document.iter_tables()]

    labelled, fee_rows, year_rows = _table_row_counts(tables)

    return LayoutQuality(
        word_count=len(WORD_PATTERN.findall(text)),
        character_count=document.character_count,
        broken_sentence_ratio=broken_sentence_ratio(text),
        table_count=len(tables),
        labelled_rows=labelled,
        fee_rows=fee_rows,
        year_value_rows=year_rows,
        multi_column_pages=_multi_column_pages(document),
        page_count=document.page_count,
        pages_with_numbers=sum(1 for page in document.pages if page.page_number is not None),
        tokens=evidence_tokens(text),
        foreign_section_ratio=(
            foreign_section_ratio(
                text=normalize_search_text(text),
                fund_name=fund_name,
            )
            if fund_name
            else 0.0
        ),
    )


# ---------------------------------------------------------------------------
# Deciding whether to try at all
# ---------------------------------------------------------------------------


# Above this share of sentence ends followed by a continuing word, the
# page was probably read across its columns rather than down them.
BROKEN_READING_ORDER_THRESHOLD: Final = 0.33

# A document holding at least this much text has something to say, so a
# parse of it that found no usable row is worth a second reading.
TEXT_WITHOUT_TABLES_CHARACTERS: Final = 2_000

# How much of a document has to be typeset in columns before the document
# counts as a column layout rather than as a page or two of them.
MULTI_COLUMN_PAGE_RATIO: Final = 0.25


def layout_triggers(
    *,
    document: ParsedDocument,
    quality: LayoutQuality | None = None,
) -> tuple[LayoutTrigger, ...]:
    """
    Return the layout problems that make a second reading worth its cost.

    A trigger only buys the document a conversion; whether the conversion
    replaces anything is decided afterwards by comparing the two.
    """

    if document.document_format is not DocumentFormat.PDF:
        return ()

    if not document.page_count:
        return ()

    measured = quality if quality is not None else measure_layout(document)

    triggers: list[LayoutTrigger] = []

    # Optical recognition is the one rung AnyDoc cannot follow, so a page
    # that only gave its text to the recogniser is left alone.
    if "ocr" not in document.parser_name and document.parser_name != "pymupdf":
        triggers.append(LayoutTrigger.PARSER_LADDER_FALLBACK)

    if measured.broken_sentence_ratio >= BROKEN_READING_ORDER_THRESHOLD:
        triggers.append(LayoutTrigger.BROKEN_READING_ORDER)

    if measured.character_count >= TEXT_WITHOUT_TABLES_CHARACTERS and not measured.labelled_rows:
        triggers.append(LayoutTrigger.TEXT_WITHOUT_TABLES)

    if measured.multi_column_ratio >= MULTI_COLUMN_PAGE_RATIO:
        triggers.append(LayoutTrigger.MULTI_COLUMN_LAYOUT)

    return tuple(triggers)


# The document types whose values the primary parser reads out of a grid
# it detected itself. AnyDoc was measured merging exactly those grids.
BLOCKED_DOCUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "priips_kid",
        "financial_statements",
    }
)


# How many per-year rows make a parse a performance or capital series
# worth protecting.
YEAR_SERIES_ROWS: Final = 5


def blocking_reason(
    *,
    document_type: str | None,
    quality: LayoutQuality,
) -> str | None:
    """
    Return why this document must keep its primary parse, or nothing.

    The guard is written on what the primary parse actually holds rather
    than on the type alone, because a fee table and a per-year series turn
    up in annual reports and factsheets that are typed as neither.
    """

    if document_type and document_type.strip().casefold() in BLOCKED_DOCUMENT_TYPES:
        return f"document type {document_type} is read from its own grid"

    if quality.fee_rows:
        return f"the primary parse already holds {quality.fee_rows} fee rows"

    if quality.year_value_rows >= YEAR_SERIES_ROWS:
        return f"the primary parse already holds {quality.year_value_rows} per-year rows"

    return None


# ---------------------------------------------------------------------------
# Putting the page numbers back
# ---------------------------------------------------------------------------


# Only a long token says anything about which page a passage came from.
ANCHOR_PATTERN: Final = re.compile(r"[0-9A-Za-zÀ-ž]{6,}")

# Below this many anchors a chunk is too short to place with confidence.
MINIMUM_CHUNK_ANCHORS: Final = 3

# How much of a chunk has to appear on a page before it is placed there.
MINIMUM_PAGE_OVERLAP: Final = 0.34

# How far behind the last placed passage a page is still looked at. Both
# readings run through the document in the same order, so the search only
# needs to step back far enough for several passages to share one page.
#
# It is not capped ahead. A cover page that cannot be placed must not stop
# the search from reaching the pages after it, which is what would happen
# if the window only ever moved when a passage was placed.
PAGE_LOOKBACK: Final = 1

# What a page nearer the last placed passage is worth over a page further
# on that matches equally well. Running text repeats itself across pages,
# and without this the second half of a document can win a passage from
# the page it was actually written on.
PAGE_DISTANCE_PENALTY: Final = 0.005


def _anchors(
    text: str,
) -> frozenset[str]:
    return frozenset(ANCHOR_PATTERN.findall(fold(text)))


def attribute_pages(
    *,
    primary: ParsedDocument,
    chunks: list[MarkdownChunk],
    document_format: DocumentFormat,
    parser_name: str = ANYDOC_FALLBACK_PARSER_NAME,
) -> ParsedDocument:
    """
    Rebuild a conversion as pages, numbered from the primary parse.

    Both readings run through the same document in the same order, so each
    passage of the conversion is looked for from the page the last one was
    placed on onwards, preferring the nearest page that matches. A passage
    that cannot be placed keeps no page number, which is what later lowers
    the confidence of a value quoted from it rather than dating it to the
    wrong page.
    """

    page_anchors = [
        (
            page.page_number,
            _anchors(page.text),
        )
        for page in primary.pages
    ]

    placements: list[int | None] = []

    pointer = 0

    for chunk in chunks:
        anchors = _anchors(chunk.text)

        if len(anchors) < MINIMUM_CHUNK_ANCHORS or not page_anchors:
            placements.append(None)
            continue

        best_index: int | None = None

        best_score = 0.0

        best_overlap = 0.0

        for index in range(max(pointer - PAGE_LOOKBACK, 0), len(page_anchors)):
            overlap = len(anchors & page_anchors[index][1]) / len(anchors)

            if overlap < MINIMUM_PAGE_OVERLAP:
                continue

            score = overlap - PAGE_DISTANCE_PENALTY * abs(index - pointer)

            if score > best_score:
                best_score = score

                best_overlap = overlap

                best_index = index

        if best_index is None or best_overlap < MINIMUM_PAGE_OVERLAP:
            placements.append(None)
            continue

        pointer = best_index

        placements.append(page_anchors[best_index][0])

    _fill_enclosed_gaps(placements)

    return _document_from_placements(
        chunks=chunks,
        placements=placements,
        document_format=document_format,
        parser_name=parser_name,
    )


def _fill_enclosed_gaps(
    placements: list[int | None],
) -> None:
    """
    Place a passage that sits between two others from the same page.

    A short paragraph between two placed ones cannot have come from
    anywhere else, and leaving it unplaced would cost the page number of
    every value quoted from it.
    """

    for index, placement in enumerate(placements):
        if placement is not None:
            continue

        previous = next(
            (placements[before] for before in range(index - 1, -1, -1) if placements[before]),
            None,
        )

        following = next(
            (placements[after] for after in range(index + 1, len(placements)) if placements[after]),
            None,
        )

        if previous is not None and previous == following:
            placements[index] = previous


def _document_from_placements(
    *,
    chunks: list[MarkdownChunk],
    placements: list[int | None],
    document_format: DocumentFormat,
    parser_name: str,
) -> ParsedDocument:
    """Group the placed passages into the pages they were placed on."""

    pages: list[ParsedPage] = []

    current_number: int | None = None

    current_texts: list[str] = []

    current_tables: list[DocumentTable] = []

    started = False

    def flush() -> None:
        if not started:
            return

        text = normalize_extracted_text("\n\n".join(current_texts))

        pages.append(
            ParsedPage(
                page_number=current_number,
                text=text,
                character_count=len(text),
                tables=tuple(current_tables),
            )
        )

        current_texts.clear()

        current_tables.clear()

    for chunk, placement in zip(chunks, placements, strict=True):
        if not started or placement != current_number:
            flush()

            current_number = placement

            started = True

        current_texts.append(chunk.text)

        if chunk.table is not None:
            current_tables.append(chunk.table)

    flush()

    kept = [page for page in pages if page.character_count]

    return ParsedDocument(
        document_format=document_format,
        parser_name=parser_name,
        pages=tuple(kept),
        character_count=sum(page.character_count for page in kept),
        scanned_candidate=not any(page.text.strip() for page in kept),
    )


# ---------------------------------------------------------------------------
# Asking the reader itself
# ---------------------------------------------------------------------------


# Given one reading of a document, return the names of the fields the
# extraction could actually deliver from it. Everything above measures
# what a reading looks like; this asks the stage that has to use it.
ExtractionVerifier = Callable[[ParsedDocument], frozenset[str]]


def extracted_field_names(
    *,
    document: ParsedDocument,
    record: ParsedDocumentRecord,
    fund_name: str,
    fund_web: str | None = None,
) -> frozenset[str]:
    """
    Return the fields the extraction delivers from one reading.

    The import is deferred because the extraction imports the parser and
    this module sits between them; nothing here runs during a normal
    parse unless a fallback is actually being weighed.
    """

    from fundscraper.extended_extraction import extract_extended_fields
    from fundscraper.field_extraction import ExtractionDocument, extract_fund_fields
    from fundscraper.output_models import FieldStatus

    extraction_document = ExtractionDocument(
        record=record,
        document=document,
    )

    core = extract_fund_fields(
        fund_name=fund_name,
        documents=[extraction_document],
    )

    extended = extract_extended_fields(
        fund_name=fund_name,
        fund_web=fund_web,
        documents=[extraction_document],
    )

    statuses: tuple[tuple[str, FieldStatus], ...] = (
        ("investment_horizon", core.investment_horizon.status),
        ("minimum_investment", core.minimum_investment.status),
        ("target_return", core.target_return.status),
        ("fees", core.fees.status),
        ("assets_under_management", core.assets_under_management.status),
        ("manager", extended.manager.status),
        ("administrator", extended.administrator.status),
        ("aum_history", extended.aum_history.status),
        ("annual_returns", extended.annual_returns.status),
        ("historical_values", extended.historical_values.status),
    )

    return frozenset(name for name, status in statuses if status is FieldStatus.FOUND)


# ---------------------------------------------------------------------------
# Deciding whether the second reading was better
# ---------------------------------------------------------------------------


# How much of the primary parse's words a replacement has to keep. A
# conversion that reads less of the document is not an improvement in
# structure, it is a loss of content wearing one.
MINIMUM_WORD_RETENTION: Final = 0.97

# How much of the dates, years and rates a replacement has to keep. These
# are what a value is attributed and dated by.
MINIMUM_EVIDENCE_RETENTION: Final = 0.95

# How much the reading order has to improve before a conversion that adds
# no rows is still worth taking.
READING_ORDER_IMPROVEMENT: Final = 0.05

# How much more of a document may read as another fund's before the
# replacement counts as having taken attribution away. The scope rules
# refuse a value found inside such a span, so this is the difference
# between a value that is delivered and one that is thrown away.
FOREIGN_SECTION_TOLERANCE: Final = 0.05


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    """What was decided about one document, and why."""

    outcome: FallbackOutcome
    triggers: tuple[LayoutTrigger, ...] = ()
    reasons: tuple[str, ...] = ()
    improvements: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    primary_quality: LayoutQuality | None = None
    candidate_quality: LayoutQuality | None = None
    conversion_seconds: float = 0.0
    document: ParsedDocument | None = None

    # What AnyDoc produced, kept whether or not it was preferred, so a
    # rejection can be reviewed rather than only counted.
    candidate: ParsedDocument | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is FallbackOutcome.ACCEPTED

    def to_json_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "triggers": [trigger.value for trigger in self.triggers],
            "reasons": list(self.reasons),
            "improvements": list(self.improvements),
            "regressions": list(self.regressions),
            "conversion_seconds": round(self.conversion_seconds, 4),
            "primary": (
                self.primary_quality.to_json_dict() if self.primary_quality is not None else None
            ),
            "candidate": (
                self.candidate_quality.to_json_dict()
                if self.candidate_quality is not None
                else None
            ),
        }


def compare_parses(
    *,
    primary: LayoutQuality,
    candidate: LayoutQuality,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return what the second reading gained and what it cost."""

    improvements: list[str] = []

    regressions: list[str] = []

    if candidate.labelled_rows > primary.labelled_rows:
        improvements.append(f"labelled rows {primary.labelled_rows} -> {candidate.labelled_rows}")

    order_gain = primary.broken_sentence_ratio - candidate.broken_sentence_ratio

    if order_gain >= READING_ORDER_IMPROVEMENT:
        improvements.append(
            f"broken sentences {primary.broken_sentence_ratio:.0%} -> "
            f"{candidate.broken_sentence_ratio:.0%}"
        )

    if not candidate.word_count:
        regressions.append("the conversion produced no words")

    word_retention = candidate.word_count / primary.word_count if primary.word_count else 1.0

    if word_retention < MINIMUM_WORD_RETENTION:
        regressions.append(f"kept only {word_retention:.0%} of the words")

    if candidate.labelled_rows < primary.labelled_rows:
        regressions.append(f"labelled rows {primary.labelled_rows} -> {candidate.labelled_rows}")

    if order_gain <= -READING_ORDER_IMPROVEMENT:
        regressions.append(
            f"broken sentences {primary.broken_sentence_ratio:.0%} -> "
            f"{candidate.broken_sentence_ratio:.0%}"
        )

    for label, primary_tokens, candidate_tokens, floor in (
        ("years", primary.tokens.years, candidate.tokens.years, MINIMUM_EVIDENCE_RETENTION),
        ("dates", primary.tokens.dates, candidate.tokens.dates, MINIMUM_EVIDENCE_RETENTION),
        (
            "percentages",
            primary.tokens.percentages,
            candidate.tokens.percentages,
            MINIMUM_EVIDENCE_RETENTION,
        ),
        # An identifier is what ties a value to one share class, and there
        # are few enough of them that losing one is losing a class.
        ("identifiers", primary.tokens.isins, candidate.tokens.isins, 1.0),
    ):
        kept = retention(
            primary=primary_tokens,
            candidate=candidate_tokens,
        )

        if kept < floor:
            regressions.append(
                f"kept only {kept:.0%} of the {len(primary_tokens)} {label} the primary parse found"
            )

    foreign_gain = candidate.foreign_section_ratio - primary.foreign_section_ratio

    if foreign_gain > FOREIGN_SECTION_TOLERANCE:
        regressions.append(
            f"the share reading as another fund's rose from "
            f"{primary.foreign_section_ratio:.0%} to {candidate.foreign_section_ratio:.0%}"
        )
    elif foreign_gain < -FOREIGN_SECTION_TOLERANCE:
        improvements.append(
            f"the share reading as another fund's fell from "
            f"{primary.foreign_section_ratio:.0%} to {candidate.foreign_section_ratio:.0%}"
        )

    return (
        tuple(improvements),
        tuple(regressions),
    )


def apply_layout_fallback(
    *,
    body: bytes,
    primary: ParsedDocument,
    document_type: str | None = None,
    fund_name: str | None = None,
    verify: ExtractionVerifier | None = None,
) -> FallbackDecision:
    """
    Read one document a second way when its layout defeated the first.

    The returned decision always names the document the caller should
    keep. When nothing better was found that is the primary parse, and
    the caller can store it unchanged.

    ``fund_name`` lets the comparison check that the second reading did
    not cost the document its attribution. ``verify`` goes further and
    asks the extraction itself what each reading yields. Without either,
    a replacement is refused rather than taken unchecked.
    """

    quality = measure_layout(primary, fund_name=fund_name)

    triggers = layout_triggers(
        document=primary,
        quality=quality,
    )

    if not triggers:
        return FallbackDecision(
            outcome=FallbackOutcome.NOT_TRIGGERED,
            primary_quality=quality,
            document=primary,
        )

    blocked = blocking_reason(
        document_type=document_type,
        quality=quality,
    )

    if blocked is not None:
        return FallbackDecision(
            outcome=FallbackOutcome.BLOCKED,
            triggers=triggers,
            reasons=(blocked,),
            primary_quality=quality,
            document=primary,
        )

    if not anydoc_available() or not anydoc_supports(primary.document_format):
        return FallbackDecision(
            outcome=FallbackOutcome.UNAVAILABLE,
            triggers=triggers,
            reasons=("AnyDoc is not installed",),
            primary_quality=quality,
            document=primary,
        )

    started = time.perf_counter()

    try:
        markdown = convert_to_markdown(
            body=body,
            document_format=primary.document_format,
        )
    except AnyDocParseError as exc:
        return FallbackDecision(
            outcome=FallbackOutcome.FAILED,
            triggers=triggers,
            reasons=(str(exc),),
            primary_quality=quality,
            conversion_seconds=time.perf_counter() - started,
            document=primary,
        )

    candidate = attribute_pages(
        primary=primary,
        chunks=markdown_chunks(markdown),
        document_format=primary.document_format,
    )

    conversion_seconds = time.perf_counter() - started

    candidate_quality = measure_layout(candidate, fund_name=fund_name)

    improvements, regressions = compare_parses(
        primary=quality,
        candidate=candidate_quality,
    )

    if fund_name is None:
        regressions = (
            *regressions,
            "the fund is not known here, so the attribution check could not run",
        )

    if verify is not None and not regressions:
        # The measurements above describe what a reading looks like. This
        # asks the stage that has to use it, which is the only test that
        # cannot be fooled by a conversion that is tidier but says less.
        primary_fields = verify(primary)

        candidate_fields = verify(candidate)

        lost_fields = sorted(primary_fields - candidate_fields)

        gained_fields = sorted(candidate_fields - primary_fields)

        if lost_fields:
            regressions = (
                *regressions,
                "the extraction would lose " + ", ".join(lost_fields),
            )

        if gained_fields:
            improvements = (
                *improvements,
                "the extraction would gain " + ", ".join(gained_fields),
            )

    if regressions or not improvements:
        return FallbackDecision(
            outcome=FallbackOutcome.REJECTED,
            triggers=triggers,
            reasons=(
                regressions if regressions else ("the second reading improved nothing measurable",)
            ),
            improvements=improvements,
            regressions=regressions,
            primary_quality=quality,
            candidate_quality=candidate_quality,
            conversion_seconds=conversion_seconds,
            document=primary,
            candidate=candidate,
        )

    return FallbackDecision(
        outcome=FallbackOutcome.ACCEPTED,
        triggers=triggers,
        reasons=improvements,
        improvements=improvements,
        regressions=(),
        primary_quality=quality,
        candidate_quality=candidate_quality,
        conversion_seconds=conversion_seconds,
        document=candidate,
        candidate=candidate,
    )
