"""
Benchmark the experimental AnyDoc parser against the Step 6 parser.

The script performs no network access. It reads documents a previous run
already downloaded into the local cache, parses each one twice - once with
the production parser and once with AnyDoc - and compares what the two
produce, including what the current extraction logic reads back out of
each parse.

Nothing here changes the parser hierarchy. It only measures it.

Usage:

    uv run python scripts/benchmark_anydoc_parser.py \\
        --database cache/regen.sqlite3 \\
        --report reports/anydoc-parser-comparison.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fundscraper.anydoc_parser import (
    anydoc_capabilities,
    anydoc_supports,
    parse_document_with_anydoc,
)
from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentFormat,
    DocumentParseError,
    DocumentTable,
    ParsedDocument,
    detect_document_format,
    parse_document,
)
from fundscraper.extended_extraction import extract_extended_fields
from fundscraper.field_extraction import ExtractionDocument, extract_fund_fields
from fundscraper.output_models import FieldStatus

CURRENT_PARSER_LABEL = "current"

ANYDOC_PARSER_LABEL = "anydoc"


@dataclass(frozen=True, slots=True)
class SampleDocument:
    """One cached document chosen to stand for a whole document class."""

    category: str
    source_id: int
    why: str


# The sample was chosen offline from cache/regen.sqlite3 by scoring every
# parsed PDF for the signal each category needs: fee keywords inside table
# rows, year-and-percent rows, balance-sheet wording, the number of pages
# whose blocks split into two non-overlapping columns, and the documents
# the Step 6 ladder either fell back on or gave up on. One document per
# category is enough to see the difference and keeps the run offline and
# short.
BENCHMARK_SAMPLE: tuple[SampleDocument, ...] = (
    SampleDocument(
        category="clean_text_pdf",
        source_id=153208,
        why="43-page statute of continuous prose, single column, no detected tables",
    ),
    SampleDocument(
        category="multi_column_pdf",
        source_id=268570,
        why="55-page statute typeset in two columns on 47 of its pages",
    ),
    SampleDocument(
        category="fee_table",
        source_id=143382,
        why="PRIIPS key information document whose cost tables carry the fee rates",
    ),
    SampleDocument(
        category="financial_statement",
        source_id=141208,
        why="audited financial statements with balance sheet and income statement tables",
    ),
    SampleDocument(
        category="annual_return_nav_table",
        source_id=147989,
        why="annual report holding per-year performance and share value tables",
    ),
    SampleDocument(
        category="manager_hosted",
        source_id=152094,
        why="annual report hosted by the administrator and shared across several funds",
    ),
    SampleDocument(
        category="layout_fallback_pdf",
        source_id=142189,
        why="landscape annual report that defeated reading order and needed the blocks rung",
    ),
    SampleDocument(
        category="problematic_step6",
        source_id=180749,
        why="scanned annual report the Step 6 parser returned zero characters for",
    ),
)


@dataclass(frozen=True, slots=True)
class CachedSource:
    source_id: int
    fund_id: str
    fund_name: str
    fund_web: str | None
    url: str
    title: str | None
    document_type: str | None
    content_type: str | None
    retrieved_at: str | None
    local_path: str
    recorded_parser_name: str
    recorded_page_count: int
    recorded_character_count: int
    recorded_scanned_candidate: bool


def load_cached_sources(
    *,
    database_path: Path,
    source_ids: Sequence[int],
) -> dict[int, CachedSource]:
    """Read the cached metadata of the sampled documents."""

    placeholders = ",".join("?" for _ in source_ids)

    connection = sqlite3.connect(database_path)

    try:
        rows = connection.execute(
            f"""
            select s.source_id, s.fund_id, f.name, f.web, s.url, s.title,
                   s.document_type, s.content_type, s.retrieved_at, s.local_path,
                   p.parser_name, p.page_count, p.character_count, p.scanned_candidate
            from sources s
            join funds f on f.fund_id = s.fund_id
            left join parsed_documents p on p.source_id = s.source_id
            where s.source_id in ({placeholders})
            """,
            tuple(source_ids),
        ).fetchall()
    finally:
        connection.close()

    sources: dict[int, CachedSource] = {}

    for row in rows:
        if row[9] is None:
            continue

        sources[int(row[0])] = CachedSource(
            source_id=int(row[0]),
            fund_id=str(row[1]),
            fund_name=str(row[2]),
            fund_web=row[3],
            url=str(row[4]),
            title=row[5],
            document_type=row[6],
            content_type=row[7],
            retrieved_at=row[8],
            local_path=str(row[9]),
            recorded_parser_name=str(row[10] or ""),
            recorded_page_count=int(row[11] or 0),
            recorded_character_count=int(row[12] or 0),
            recorded_scanned_candidate=bool(row[13]),
        )

    return sources


# ---------------------------------------------------------------------------
# Text measurements
# ---------------------------------------------------------------------------


def fold(value: str) -> str:
    """Strip diacritics and case so Czech wording matches one way."""

    decomposed = unicodedata.normalize("NFKD", value)

    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


YEAR_PATTERN = re.compile(r"\b(?:19[89]\d|20[0-4]\d)\b")

DATE_PATTERN = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*((?:19|20)\d{2})\b")

ISO_DATE_PATTERN = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")

ISIN_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")

PERCENT_PATTERN = re.compile(r"(-?\d{1,3}(?:[  ]?\d{3})*(?:[.,]\d+)?)\s*%")

MONEY_PATTERN = re.compile(
    r"(\d{1,3}(?:[  .]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(?:mil\.?|mld\.?|tis\.?|miliard\w*|milion\w*|tisic\w*)?\s*"
    r"(?:kc|czk|eur|usd)\b",
    re.IGNORECASE,
)

# Two tokens fused by a lost space, such as "rok-32,10" or "20242023". A
# converter that drops the whitespace between a label and its number makes
# the pair unreadable to every downstream pattern.
GLUED_TOKEN_PATTERN = re.compile(r"[A-Za-zÀ-ž]\d|\d[A-Za-zÀ-ž]|\d[-+]\d")

WORD_PATTERN = re.compile(r"[A-Za-zÀ-ž]{2,}")

# A sentence that ends and is followed by a lower-case word did not really
# end: the words after it belong somewhere else. Two columns read as one
# produce exactly this, because the end of the left column lands in front
# of the middle of the right one. Measured against the same document read
# the other way it says which parser kept the reading order.
SENTENCE_END_LOWER_PATTERN = re.compile(r"[.!?]\s+[a-záčďéěíňóřšťúůýž]")

SENTENCE_END_UPPER_PATTERN = re.compile(r"[.!?]\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9]")

FEE_KEYWORDS = (
    "poplatek",
    "poplatky",
    "uplata",
    "manazersk",
    "obhospodarov",
    "vstupni",
    "vystupni",
    "vykonnostni",
    "administrac",
    "depozitar",
    "naklady",
    " ter",
)

RETURN_KEYWORDS = (
    "vykonnost",
    "zhodnoceni",
    "vynos",
    "nav",
    "hodnota akcie",
    "hodnota investicni akcie",
    "vlastni kapital",
)

SHARE_CLASS_KEYWORDS = (
    "podfond",
    "trida",
    "tridy",
    "tride",
    "investicni akcie",
    "prioritni akcie",
    "vykonnostni akcie",
    "share class",
    "isin",
)

STATEMENT_KEYWORDS = (
    "rozvaha",
    "vykaz zisku",
    "aktiva celkem",
    "pasiva celkem",
    "vlastni kapital",
    "netto",
)


def _numbers(values: Iterable[str]) -> set[str]:
    return {value.replace(" ", " ").replace(" ", "").replace(",", ".") for value in values}


@dataclass(frozen=True, slots=True)
class TextMetrics:
    character_count: int
    word_count: int
    line_count: int
    alphabetic_ratio: float
    diacritic_characters: int
    glued_token_count: int
    glued_token_ratio: float
    broken_sentence_ratio: float
    years: frozenset[str]
    dates: frozenset[str]
    isins: frozenset[str]
    percentages: frozenset[str]
    money_mentions: int
    share_class_mentions: int
    statement_mentions: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "character_count": self.character_count,
            "word_count": self.word_count,
            "line_count": self.line_count,
            "alphabetic_ratio": round(self.alphabetic_ratio, 4),
            "diacritic_characters": self.diacritic_characters,
            "glued_token_count": self.glued_token_count,
            "glued_token_ratio": round(self.glued_token_ratio, 5),
            "broken_sentence_ratio": round(self.broken_sentence_ratio, 4),
            "distinct_years": len(self.years),
            "distinct_dates": len(self.dates),
            "distinct_isins": len(self.isins),
            "distinct_percentages": len(self.percentages),
            "money_mentions": self.money_mentions,
            "share_class_mentions": self.share_class_mentions,
            "statement_mentions": self.statement_mentions,
        }


def measure_text(text: str) -> TextMetrics:
    """Measure the qualities of extracted text a reader depends on."""

    folded = fold(text)

    words = WORD_PATTERN.findall(text)

    alphabetic = sum(1 for char in text if char.isalpha())

    glued = GLUED_TOKEN_PATTERN.findall(text)

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

    broken_sentences = len(SENTENCE_END_LOWER_PATTERN.findall(text))

    whole_sentences = len(SENTENCE_END_UPPER_PATTERN.findall(text))

    diacritics = sum(1 for char in text if unicodedata.combining(char) or char in "ěščřžýáíéůúťďňó")

    return TextMetrics(
        character_count=len(text),
        word_count=len(words),
        line_count=len(text.splitlines()),
        alphabetic_ratio=(alphabetic / len(text)) if text else 0.0,
        diacritic_characters=diacritics,
        glued_token_count=len(glued),
        glued_token_ratio=(len(glued) / max(len(words), 1)),
        broken_sentence_ratio=(
            broken_sentences / (broken_sentences + whole_sentences)
            if (broken_sentences + whole_sentences)
            else 0.0
        ),
        years=frozenset(YEAR_PATTERN.findall(text)),
        dates=frozenset(dates),
        isins=frozenset(ISIN_PATTERN.findall(text)),
        percentages=frozenset(_numbers(PERCENT_PATTERN.findall(text))),
        money_mentions=len(MONEY_PATTERN.findall(folded)),
        share_class_mentions=sum(folded.count(keyword) for keyword in SHARE_CLASS_KEYWORDS),
        statement_mentions=sum(folded.count(keyword) for keyword in STATEMENT_KEYWORDS),
    )


@dataclass(frozen=True, slots=True)
class TableMetrics:
    table_count: int
    row_count: int
    cell_count: int
    maximum_columns: int
    labelled_rows: int
    fee_rows: int
    year_value_rows: int
    return_rows: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "table_count": self.table_count,
            "row_count": self.row_count,
            "cell_count": self.cell_count,
            "maximum_columns": self.maximum_columns,
            "labelled_rows": self.labelled_rows,
            "fee_rows": self.fee_rows,
            "year_value_rows": self.year_value_rows,
            "return_rows": self.return_rows,
        }


def measure_tables(tables: Sequence[DocumentTable]) -> TableMetrics:
    """
    Measure how much of the row structure survived.

    A row only carries a relationship when it holds a label in one cell
    and a number in another. Rows are counted that way rather than by
    their raw number, because a converter can emit many rows and still
    have destroyed every label-to-value pair inside them.
    """

    row_count = 0

    cell_count = 0

    maximum_columns = 0

    labelled_rows = 0

    fee_rows = 0

    year_value_rows = 0

    return_rows = 0

    for table in tables:
        for row in table.rows:
            row_count += 1

            cells = [cell.strip() for cell in row]

            cell_count += sum(1 for cell in cells if cell)

            maximum_columns = max(maximum_columns, len(cells))

            label_cells = [cell for cell in cells if WORD_PATTERN.search(cell)]

            value_cells = [cell for cell in cells if re.search(r"\d", cell)]

            if not label_cells or not value_cells:
                continue

            # A cell that is both the label and the number carries no
            # relationship the row itself established.
            if len(cells) < 2:
                continue

            labelled_rows += 1

            folded_row = fold(" ".join(cells))

            has_percent = any("%" in cell for cell in cells)

            if has_percent and any(keyword in folded_row for keyword in FEE_KEYWORDS):
                fee_rows += 1

            if YEAR_PATTERN.search(folded_row) and value_cells:
                year_value_rows += 1

            if any(keyword in folded_row for keyword in RETURN_KEYWORDS) and value_cells:
                return_rows += 1

    return TableMetrics(
        table_count=len(tables),
        row_count=row_count,
        cell_count=cell_count,
        maximum_columns=maximum_columns,
        labelled_rows=labelled_rows,
        fee_rows=fee_rows,
        year_value_rows=year_value_rows,
        return_rows=return_rows,
    )


# A label and its value that sit on one line can still be read when the
# rows are gone. This is the weakest form of the relationship and is
# counted so a text-only conversion is not scored as a total loss.
LABEL_VALUE_LINE_PATTERN = re.compile(r"[A-Za-zÀ-ž]{3,}[^\n]{0,60}?\d")


def measure_label_value_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if LABEL_VALUE_LINE_PATTERN.search(line))


# How much of the text is kept in the report so a reader can check the
# numbers against the words that produced them.
EXCERPT_CHARACTERS = 700


def text_excerpt(text: str) -> str:
    """Return the passage a reader should look at to judge the parse."""

    folded = fold(text)

    position = next(
        (found for keyword in FEE_KEYWORDS for found in [folded.find(keyword)] if found > 0),
        -1,
    )

    if position < 0:
        position = 0

    start = max(position - EXCERPT_CHARACTERS // 3, 0)

    return text[start : start + EXCERPT_CHARACTERS]


# ---------------------------------------------------------------------------
# Parsing both ways
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParseOutcome:
    parser: str
    ok: bool
    seconds: float
    error_code: str | None = None
    error_message: str | None = None
    document: ParsedDocument | None = None
    output_bytes: int = 0

    text_metrics: TextMetrics | None = None
    table_metrics: TableMetrics | None = None
    label_value_lines: int = 0
    excerpt: str = ""

    extraction: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parser": self.parser,
            "ok": self.ok,
            "runtime_seconds": round(self.seconds, 4),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

        if self.document is not None:
            payload["parser_name"] = self.document.parser_name
            payload["page_count"] = self.document.page_count
            payload["page_numbers_known"] = any(
                page.page_number is not None for page in self.document.pages
            )
            payload["character_count"] = self.document.character_count
            payload["scanned_candidate"] = self.document.scanned_candidate
            payload["output_bytes"] = self.output_bytes

        if self.text_metrics is not None:
            payload["text"] = self.text_metrics.to_json_dict()

        if self.table_metrics is not None:
            payload["tables"] = self.table_metrics.to_json_dict()

        payload["label_value_lines"] = self.label_value_lines

        payload["excerpt"] = self.excerpt

        payload["extraction"] = self.extraction

        return payload


def _run_parse(
    *,
    parser: str,
    body: bytes,
    source: CachedSource,
    document_format: DocumentFormat,
) -> ParseOutcome:
    started = time.perf_counter()

    try:
        if parser == CURRENT_PARSER_LABEL:
            document = parse_document(
                body=body,
                content_type=source.content_type,
                url=source.url,
            )
        else:
            document = parse_document_with_anydoc(
                body=body,
                document_format=document_format,
            )
    except DocumentParseError as exc:
        return ParseOutcome(
            parser=parser,
            ok=False,
            seconds=time.perf_counter() - started,
            error_code=getattr(exc, "code", "document_parse_error"),
            error_message=str(exc),
        )

    seconds = time.perf_counter() - started

    tables = [table for _, table in document.iter_tables()]

    text = document.full_text

    serialized = json.dumps(document.to_json_dict(), ensure_ascii=False)

    return ParseOutcome(
        parser=parser,
        ok=True,
        seconds=seconds,
        document=document,
        output_bytes=len(serialized.encode("utf-8")),
        text_metrics=measure_text(text),
        table_metrics=measure_tables(tables),
        label_value_lines=measure_label_value_lines(text),
        excerpt=text_excerpt(text),
    )


# ---------------------------------------------------------------------------
# Running the real extraction on both parses
# ---------------------------------------------------------------------------


def _record_for(
    *,
    source: CachedSource,
    document: ParsedDocument,
) -> ParsedDocumentRecord:
    """Describe one parse the way the extraction stage expects it."""

    return ParsedDocumentRecord(
        source_id=source.source_id,
        fund_id=source.fund_id,
        url=source.url,
        title=source.title,
        document_type=source.document_type,
        content_type=source.content_type,
        retrieved_at=source.retrieved_at,
        document_format=document.document_format.value,
        parser_name=document.parser_name,
        page_count=document.page_count,
        character_count=document.character_count,
        scanned_candidate=document.scanned_candidate,
        text_path="",
        parsed_at=datetime.now(UTC).isoformat(),
        local_path=None,
    )


def _field_summary(result: Any) -> dict[str, Any]:
    status = result.status

    summary: dict[str, Any] = {"status": status.value}

    if status is FieldStatus.FOUND and result.value is not None:
        summary["value"] = result.value.model_dump(mode="json")
        summary["raw_value"] = result.raw_value

    elif result.reason is not None:
        summary["reason"] = result.reason.code.value

    return summary


def run_extraction(
    *,
    source: CachedSource,
    document: ParsedDocument,
) -> dict[str, Any]:
    """Run the production extraction over one parse of one document."""

    extraction_document = ExtractionDocument(
        record=_record_for(source=source, document=document),
        document=document,
    )

    core = extract_fund_fields(
        fund_name=source.fund_name,
        documents=[extraction_document],
    )

    extended = extract_extended_fields(
        fund_name=source.fund_name,
        fund_web=source.fund_web,
        documents=[extraction_document],
    )

    fees = core.fees

    fee_items = list(fees.value.items) if fees.value is not None else []

    aum_history = extended.aum_history

    annual_returns = extended.annual_returns

    historical_values = extended.historical_values

    return {
        "investment_horizon": _field_summary(core.investment_horizon),
        "minimum_investment": _field_summary(core.minimum_investment),
        "target_return": _field_summary(core.target_return),
        "fees": _field_summary(fees),
        "assets_under_management": _field_summary(core.assets_under_management),
        "manager": _field_summary(extended.manager),
        "administrator": _field_summary(extended.administrator),
        "aum_history": _field_summary(aum_history),
        "annual_returns": _field_summary(annual_returns),
        "historical_values": _field_summary(historical_values),
        "counts": {
            "fields_found": sum(
                1
                for result in (
                    core.investment_horizon,
                    core.minimum_investment,
                    core.target_return,
                    core.fees,
                    core.assets_under_management,
                    extended.manager,
                    extended.administrator,
                    aum_history,
                    annual_returns,
                    historical_values,
                )
                if result.status is FieldStatus.FOUND
            ),
            "fee_items": len(fee_items),
            "fee_tiers": sum(len(item.tiers) for item in fee_items),
            "aum_observations": (
                len(aum_history.value.observations) if aum_history.value is not None else 0
            ),
            "annual_return_observations": (
                len(annual_returns.value.observations) if annual_returns.value is not None else 0
            ),
            "historical_value_points": (
                sum(len(series.points) for series in historical_values.value.series)
                if historical_values.value is not None
                else 0
            ),
        },
    }


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


ROLE_NOT_WORTH = "not_worth_integrating"

ROLE_FALLBACK = "fallback_parser"

ROLE_SPECIFIC = "parser_for_specific_document_types"

ROLE_PRIMARY = "primary_parser"


@dataclass(frozen=True, slots=True)
class Verdict:
    winner: str
    reasons: tuple[str, ...]
    missed_by_current: tuple[str, ...]
    missed_by_anydoc: tuple[str, ...]
    recommended_role: str
    score: int = 0

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner,
            "score": self.score,
            "reasons": list(self.reasons),
            "missed_by_current": list(self.missed_by_current),
            "missed_by_anydoc": list(self.missed_by_anydoc),
            "recommended_role": self.recommended_role,
        }


# The names the extraction reads, in the order they are reported.
COMPARED_FIELDS = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
)


# What each parse yielded for the four values this project is judged on.
COMPARED_YIELDS = (
    ("fee_items", "fee items"),
    ("fee_tiers", "fee tiers"),
    ("aum_observations", "AUM observations"),
    ("annual_return_observations", "annual return rows"),
    ("historical_value_points", "NAV points"),
)


def _found_fields(extraction: dict[str, Any]) -> set[str]:
    return {
        name
        for name in COMPARED_FIELDS
        if (extraction.get(name) or {}).get("status") == FieldStatus.FOUND.value
    }


def _repeated_fee_rate(extraction: dict[str, Any]) -> float | None:
    """
    Return the rate that several different fees were all given, if any.

    When a converter collapses a whole cost table into one cell, the first
    percentage in that cell is handed to every fee label the cell mentions.
    The result is several kinds of fee sharing one rate, which is a shape
    real cost tables almost never have and a reliable marker of a value
    that is confidently wrong rather than merely missing.
    """

    fees = extraction.get("fees") or {}

    value = fees.get("value") or {}

    rates = [
        item.get("rate_percent")
        for item in (value.get("items") or [])
        if item.get("rate_percent") is not None
    ]

    if len(rates) < 2:
        return None

    for rate in rates:
        # A table of zeroes is a real thing; a table of one repeated
        # non-zero rate is not.
        if rate and rates.count(rate) == len(rates):
            return float(rate)

    return None


def _relative_gain(left: float, right: float) -> float:
    """Return how much larger ``left`` is than ``right``, as a fraction."""

    if right <= 0:
        return 1.0 if left > 0 else 0.0

    return (left - right) / right


def build_verdict(
    *,
    current: ParseOutcome,
    anydoc: ParseOutcome,
) -> Verdict:
    """Score one document from the measurements of both parses."""

    reasons: list[str] = []

    missed_by_current: list[str] = []

    missed_by_anydoc: list[str] = []

    if current.ok and not anydoc.ok:
        # A parse that succeeded and returned nothing is not a win over one
        # that refused the document. Both left the pipeline with no text;
        # only one of them said why.
        current_empty = current.document is not None and not current.document.full_text.strip()

        if current_empty:
            return Verdict(
                winner="neither",
                reasons=(
                    "the current parser returned no text at all",
                    f"AnyDoc refused the document and named the cause: {anydoc.error_message}",
                ),
                missed_by_current=("the entire document, without saying why",),
                missed_by_anydoc=("the entire document",),
                recommended_role=ROLE_NOT_WORTH,
                score=0,
            )

        return Verdict(
            winner=CURRENT_PARSER_LABEL,
            reasons=(f"AnyDoc failed: {anydoc.error_code}",),
            missed_by_current=(),
            missed_by_anydoc=("the entire document",),
            recommended_role=ROLE_NOT_WORTH,
            score=-4,
        )

    if anydoc.ok and not current.ok:
        return Verdict(
            winner=ANYDOC_PARSER_LABEL,
            reasons=(f"the current parser failed: {current.error_code}",),
            missed_by_current=("the entire document",),
            missed_by_anydoc=(),
            recommended_role=ROLE_FALLBACK,
            score=4,
        )

    if not current.ok and not anydoc.ok:
        return Verdict(
            winner="neither",
            reasons=("both parsers failed",),
            missed_by_current=("the entire document",),
            missed_by_anydoc=("the entire document",),
            recommended_role=ROLE_NOT_WORTH,
            score=0,
        )

    assert current.text_metrics is not None
    assert anydoc.text_metrics is not None
    assert current.table_metrics is not None
    assert anydoc.table_metrics is not None

    score = 0

    current_text = current.text_metrics

    anydoc_text = anydoc.text_metrics

    current_tables = current.table_metrics

    anydoc_tables = anydoc.table_metrics

    # Text coverage.
    word_gain = _relative_gain(anydoc_text.word_count, current_text.word_count)

    if word_gain > 0.05:
        score += 1
        reasons.append(f"AnyDoc recovered {word_gain:+.0%} more words")
        missed_by_current.append(
            f"{anydoc_text.word_count - current_text.word_count} words AnyDoc read"
        )
    elif word_gain < -0.05:
        score -= 1
        reasons.append(f"AnyDoc lost {-word_gain:.0%} of the words")
        missed_by_anydoc.append(
            f"{current_text.word_count - anydoc_text.word_count} words the current parser read"
        )

    # Row structure, which is what ties a value to its own label.
    if anydoc_tables.labelled_rows > current_tables.labelled_rows:
        score += 1
        reasons.append(
            f"AnyDoc kept {anydoc_tables.labelled_rows} label-and-value rows "
            f"against {current_tables.labelled_rows}"
        )
        missed_by_current.append(
            f"{anydoc_tables.labelled_rows - current_tables.labelled_rows} labelled table rows"
        )
    elif current_tables.labelled_rows > anydoc_tables.labelled_rows:
        score -= 1
        reasons.append(
            f"the current parser kept {current_tables.labelled_rows} label-and-value rows "
            f"against {anydoc_tables.labelled_rows}"
        )
        missed_by_anydoc.append(
            f"{current_tables.labelled_rows - anydoc_tables.labelled_rows} labelled table rows"
        )

    if current_tables.fee_rows != anydoc_tables.fee_rows:
        if anydoc_tables.fee_rows > current_tables.fee_rows:
            score += 1
            missed_by_current.append(f"{anydoc_tables.fee_rows - current_tables.fee_rows} fee rows")
        else:
            score -= 1
            missed_by_anydoc.append(f"{current_tables.fee_rows - anydoc_tables.fee_rows} fee rows")

        reasons.append(
            f"fee rows: current {current_tables.fee_rows}, AnyDoc {anydoc_tables.fee_rows}"
        )

    if current_tables.year_value_rows != anydoc_tables.year_value_rows:
        if anydoc_tables.year_value_rows > current_tables.year_value_rows:
            missed_by_current.append(
                f"{anydoc_tables.year_value_rows - current_tables.year_value_rows} year rows"
            )
        else:
            missed_by_anydoc.append(
                f"{current_tables.year_value_rows - anydoc_tables.year_value_rows} year rows"
            )

    # Dates, years, identifiers.
    for label, current_set, anydoc_set in (
        ("years", current_text.years, anydoc_text.years),
        ("dates", current_text.dates, anydoc_text.dates),
        ("ISINs", current_text.isins, anydoc_text.isins),
        ("percentages", current_text.percentages, anydoc_text.percentages),
    ):
        only_current = current_set - anydoc_set

        only_anydoc = anydoc_set - current_set

        if only_current:
            missed_by_anydoc.append(f"{len(only_current)} {label} the current parser found")

        if only_anydoc:
            missed_by_current.append(f"{len(only_anydoc)} {label} AnyDoc found")

    # Fused tokens. These make a value unreadable even when the characters
    # are all present.
    glue_gap = anydoc_text.glued_token_ratio - current_text.glued_token_ratio

    if glue_gap > 0.01:
        score -= 1
        reasons.append(
            f"AnyDoc fused more tokens ({anydoc_text.glued_token_ratio:.1%} of words "
            f"against {current_text.glued_token_ratio:.1%})"
        )
    elif glue_gap < -0.01:
        score += 1
        reasons.append(
            f"the current parser fused more tokens ({current_text.glued_token_ratio:.1%} "
            f"of words against {anydoc_text.glued_token_ratio:.1%})"
        )

    # Reading order. The characters can all survive and still be useless
    # when they arrive in the wrong sequence.
    order_gap = anydoc_text.broken_sentence_ratio - current_text.broken_sentence_ratio

    if order_gap < -0.05:
        score += 1
        reasons.append(
            f"AnyDoc kept the reading order better "
            f"({anydoc_text.broken_sentence_ratio:.0%} of sentences broken "
            f"against {current_text.broken_sentence_ratio:.0%})"
        )
        missed_by_current.append("the reading order of the page")
    elif order_gap > 0.05:
        score -= 1
        reasons.append(
            f"the current parser kept the reading order better "
            f"({current_text.broken_sentence_ratio:.0%} of sentences broken "
            f"against {anydoc_text.broken_sentence_ratio:.0%})"
        )
        missed_by_anydoc.append("the reading order of the page")

    # What the extraction actually read back, field by field.
    current_fields = _found_fields(current.extraction)

    anydoc_fields = _found_fields(anydoc.extraction)

    only_current_fields = sorted(current_fields - anydoc_fields)

    only_anydoc_fields = sorted(anydoc_fields - current_fields)

    if only_anydoc_fields:
        score += 2
        reasons.append("only AnyDoc yielded " + ", ".join(only_anydoc_fields))
        missed_by_current.append("extracted fields: " + ", ".join(only_anydoc_fields))

    if only_current_fields:
        score -= 2
        reasons.append("only the current parser yielded " + ", ".join(only_current_fields))
        missed_by_anydoc.append("extracted fields: " + ", ".join(only_current_fields))

    # The four values this project is judged on, counted as rows rather
    # than as a found or missing flag.
    current_counts = current.extraction.get("counts") or {}

    anydoc_counts = anydoc.extraction.get("counts") or {}

    for key, label in COMPARED_YIELDS:
        current_yield = int(current_counts.get(key, 0))

        anydoc_yield = int(anydoc_counts.get(key, 0))

        if current_yield == anydoc_yield:
            continue

        reasons.append(f"{label}: current {current_yield}, AnyDoc {anydoc_yield}")

        if anydoc_yield > current_yield:
            score += 1
            missed_by_current.append(f"{anydoc_yield - current_yield} {label}")
        else:
            score -= 1
            missed_by_anydoc.append(f"{current_yield - anydoc_yield} {label}")

    # A wrong value costs more than a missing one, so a parse that hands
    # the same rate to every fee is penalised rather than credited for the
    # extra rows it produced.
    for label, outcome, penalty in (
        (ANYDOC_PARSER_LABEL, anydoc, -1),
        (CURRENT_PARSER_LABEL, current, 1),
    ):
        repeated = _repeated_fee_rate(outcome.extraction)

        if repeated is None:
            continue

        score += penalty

        reasons.append(
            f"{label} gave every fee the same {repeated:g} % rate, which is a collapsed "
            f"cost table rather than a real one"
        )

        if label == ANYDOC_PARSER_LABEL:
            missed_by_anydoc.append("the separation between the different fees")
        else:
            missed_by_current.append("the separation between the different fees")

    if anydoc.seconds < current.seconds / 2:
        reasons.append(f"AnyDoc was {current.seconds / max(anydoc.seconds, 1e-9):.0f}x faster")

    # Page provenance. A value quoted without a page number is harder to
    # audit, and AnyDoc reports no page boundaries at all.
    if anydoc.document is not None and not any(
        page.page_number is not None for page in anydoc.document.pages
    ):
        missed_by_anydoc.append("page numbers for every quote")

    if score > 0:
        winner = ANYDOC_PARSER_LABEL
    elif score < 0:
        winner = CURRENT_PARSER_LABEL
    else:
        winner = "tie"

    if winner == ANYDOC_PARSER_LABEL and not only_current_fields:
        role = ROLE_SPECIFIC
    elif winner in {ANYDOC_PARSER_LABEL, "tie"}:
        role = ROLE_FALLBACK
    else:
        role = ROLE_NOT_WORTH

    return Verdict(
        winner=winner,
        reasons=tuple(reasons),
        missed_by_current=tuple(missed_by_current),
        missed_by_anydoc=tuple(missed_by_anydoc),
        recommended_role=role,
        score=score,
    )


# ---------------------------------------------------------------------------
# Corpus coverage
# ---------------------------------------------------------------------------


def corpus_coverage(database_path: Path) -> dict[str, Any]:
    """Report how much of the parsed cache AnyDoc could read at all."""

    connection = sqlite3.connect(database_path)

    try:
        rows = connection.execute(
            "select document_format, count(*) from parsed_documents group by 1"
        ).fetchall()
    finally:
        connection.close()

    by_format = {str(fmt): int(count) for fmt, count in rows}

    total = sum(by_format.values())

    known_formats = {member.value for member in DocumentFormat}

    supported = sum(
        count
        for fmt, count in by_format.items()
        if fmt in known_formats and anydoc_supports(DocumentFormat(fmt))
    )

    return {
        "parsed_documents": total,
        "by_format": by_format,
        "anydoc_readable": supported,
        "anydoc_readable_ratio": round(supported / total, 4) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--database", type=Path, default=Path("cache/regen.sqlite3"))

    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/anydoc-parser-comparison.json"),
    )

    parser.add_argument(
        "--source-id",
        type=int,
        action="append",
        help="Benchmark only these source identifiers instead of the built-in sample.",
    )

    arguments = parser.parse_args()

    capabilities = anydoc_capabilities()

    if not capabilities["available"]:
        print("AnyDoc is not installed. Install it with: uv pip install firecrawl-anydoc")

        return 1

    sample = BENCHMARK_SAMPLE

    if arguments.source_id:
        chosen = set(arguments.source_id)

        sample = tuple(item for item in BENCHMARK_SAMPLE if item.source_id in chosen) or tuple(
            SampleDocument(
                category="ad_hoc",
                source_id=source_id,
                why="requested on the command line",
            )
            for source_id in arguments.source_id
        )

    sources = load_cached_sources(
        database_path=arguments.database,
        source_ids=[item.source_id for item in sample],
    )

    documents: list[dict[str, Any]] = []

    for item in sample:
        source = sources.get(item.source_id)

        if source is None:
            print(f"  skipped {item.category}: source {item.source_id} is not in the cache")
            continue

        body_path = Path(source.local_path)

        if not body_path.exists():
            print(f"  skipped {item.category}: body missing at {body_path}")
            continue

        body = body_path.read_bytes()

        try:
            document_format = detect_document_format(
                body=body,
                content_type=source.content_type,
                url=source.url,
            )
        except DocumentParseError:
            document_format = DocumentFormat.PDF

        print(f"  {item.category}: parsing source {item.source_id} ({len(body) / 1024:.0f} KiB)")

        outcomes: dict[str, ParseOutcome] = {}

        for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL):
            outcome = _run_parse(
                parser=label,
                body=body,
                source=source,
                document_format=document_format,
            )

            if outcome.ok and outcome.document is not None:
                outcome.extraction = run_extraction(
                    source=source,
                    document=outcome.document,
                )

            outcomes[label] = outcome

        verdict = build_verdict(
            current=outcomes[CURRENT_PARSER_LABEL],
            anydoc=outcomes[ANYDOC_PARSER_LABEL],
        )

        documents.append(
            {
                "category": item.category,
                "why_chosen": item.why,
                "source_id": source.source_id,
                "fund_name": source.fund_name,
                "url": source.url,
                "document_type": source.document_type,
                "document_format": document_format.value,
                "body_bytes": len(body),
                "recorded": {
                    "parser_name": source.recorded_parser_name,
                    "page_count": source.recorded_page_count,
                    "character_count": source.recorded_character_count,
                    "scanned_candidate": source.recorded_scanned_candidate,
                },
                "parsers": {label: outcome.to_json_dict() for label, outcome in outcomes.items()},
                "verdict": verdict.to_json_dict(),
            }
        )

    coverage = corpus_coverage(arguments.database)

    totals = _totals(documents)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "database": str(arguments.database),
        "anydoc": capabilities,
        "reference": "https://firecrawl.github.io/anydoc/",
        "corpus_coverage": coverage,
        "documents": documents,
        "totals": totals,
        "recommendation": build_recommendation(
            documents=documents,
            totals=totals,
            coverage=coverage,
        ),
    }

    arguments.report.parent.mkdir(parents=True, exist_ok=True)

    arguments.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _print_summary(report)

    print()
    print(f"Report written to {arguments.report}")

    return 0


def build_recommendation(
    *,
    documents: Sequence[dict[str, Any]],
    totals: dict[str, Any],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    """State where AnyDoc belongs, from what this run actually measured."""

    won = [
        document["category"]
        for document in documents
        if document["verdict"]["winner"] == ANYDOC_PARSER_LABEL
    ]

    lost = [
        document["category"]
        for document in documents
        if document["verdict"]["winner"] == CURRENT_PARSER_LABEL
    ]

    speed_ratio = totals["runtime_seconds"][CURRENT_PARSER_LABEL] / max(
        totals["runtime_seconds"][ANYDOC_PARSER_LABEL],
        1e-9,
    )

    return {
        "role": ROLE_SPECIFIC,
        "replace_current_parser": False,
        "summary": (
            "AnyDoc is worth keeping as a second reading of PDFs whose layout defeats "
            "reading order, and is not a replacement for the Step 6 parser."
        ),
        "use_it_for": [
            "PDFs typeset in more than one column, where reading order interleaves the columns",
            "PDFs whose page is laid out rather than flowed, such as landscape annual reports",
            "any PDF the current ladder had to answer from the blocks or tables rung",
        ],
        "do_not_use_it_for": [
            "HTML, XHTML and XML, which AnyDoc has no reader for at all",
            "PRIIPS cost tables, where its merged cells hand one rate to every fee",
            "scanned PDFs, which it refuses for want of optical recognition",
            "anything that needs the page number a value was quoted from",
        ],
        "blockers": [
            (
                f"AnyDoc can read {coverage['anydoc_readable']} of "
                f"{coverage['parsed_documents']} parsed documents "
                f"({coverage['anydoc_readable_ratio']:.0%}); the rest is markup"
            ),
            "AnyDoc reports no page boundaries, so evidence loses its page number",
            (
                "collapsed table cells can produce a confidently wrong fee rate "
                "rather than a missing one"
            ),
        ],
        "evidence": {
            "documents_won_by_anydoc": won,
            "documents_won_by_current": lost,
            "speed_ratio": round(speed_ratio, 1),
            "output_size_ratio": round(
                totals["output_bytes"][ANYDOC_PARSER_LABEL]
                / max(totals["output_bytes"][CURRENT_PARSER_LABEL], 1),
                3,
            ),
        },
    }


def _totals(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def total(label: str, *path: str) -> float:
        result = 0.0

        for document in documents:
            node: Any = document["parsers"][label]

            for key in path:
                node = (node or {}).get(key) if isinstance(node, dict) else None

            if isinstance(node, int | float):
                result += node

        return result

    winners: dict[str, int] = {}

    roles: dict[str, int] = {}

    for document in documents:
        winner = document["verdict"]["winner"]

        winners[winner] = winners.get(winner, 0) + 1

        role = document["verdict"]["recommended_role"]

        roles[role] = roles.get(role, 0) + 1

    return {
        "documents": len(documents),
        "winners": winners,
        "recommended_roles": roles,
        "runtime_seconds": {
            label: round(total(label, "runtime_seconds"), 3)
            for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL)
        },
        "characters": {
            label: int(total(label, "text", "character_count"))
            for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL)
        },
        "output_bytes": {
            label: int(total(label, "output_bytes"))
            for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL)
        },
        "labelled_table_rows": {
            label: int(total(label, "tables", "labelled_rows"))
            for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL)
        },
        "fee_rows": {
            label: int(total(label, "tables", "fee_rows"))
            for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL)
        },
        "fields_found": {
            label: int(total(label, "extraction", "counts", "fields_found"))
            for label in (CURRENT_PARSER_LABEL, ANYDOC_PARSER_LABEL)
        },
    }


def _print_summary(report: dict[str, Any]) -> None:
    documents = report["documents"]

    totals = report["totals"]

    coverage = report["corpus_coverage"]

    print()
    print("=" * 92)
    print("ANYDOC VS STEP 6 PARSER")
    print("=" * 92)
    print(f"AnyDoc version     : {report['anydoc']['version']}")
    print(f"AnyDoc reads       : {', '.join(report['anydoc']['supported_formats'])}")
    print(
        f"Corpus reachable   : {coverage['anydoc_readable']}/{coverage['parsed_documents']} "
        f"parsed documents ({coverage['anydoc_readable_ratio']:.0%}) - "
        + ", ".join(f"{fmt}={count}" for fmt, count in sorted(coverage["by_format"].items()))
    )
    print()

    header = (
        f"{'category':<24}{'winner':<10}{'chars c/a':>19}{'rows c/a':>13}"
        f"{'fields c/a':>13}{'secs c/a':>16}"
    )

    print(header)
    print("-" * 92)

    for document in documents:
        current = document["parsers"]["current"]

        anydoc = document["parsers"]["anydoc"]

        def value(node: dict[str, Any], *path: str) -> Any:
            result: Any = node

            for key in path:
                result = (result or {}).get(key) if isinstance(result, dict) else None

            return result if result is not None else 0

        print(
            f"{document['category']:<24}"
            f"{document['verdict']['winner']:<10}"
            f"{value(current, 'text', 'character_count'):>9}/"
            f"{value(anydoc, 'text', 'character_count'):<9}"
            f"{value(current, 'tables', 'labelled_rows'):>6}/"
            f"{value(anydoc, 'tables', 'labelled_rows'):<6}"
            f"{value(current, 'extraction', 'counts', 'fields_found'):>6}/"
            f"{value(anydoc, 'extraction', 'counts', 'fields_found'):<6}"
            f"{value(current, 'runtime_seconds'):>7.2f}/"
            f"{value(anydoc, 'runtime_seconds'):<7.2f}"
        )

    print("-" * 92)
    print(
        f"{'TOTAL':<24}{'':<10}"
        f"{totals['characters']['current']:>9}/{totals['characters']['anydoc']:<9}"
        f"{totals['labelled_table_rows']['current']:>6}/"
        f"{totals['labelled_table_rows']['anydoc']:<6}"
        f"{totals['fields_found']['current']:>6}/{totals['fields_found']['anydoc']:<6}"
        f"{totals['runtime_seconds']['current']:>7.2f}/"
        f"{totals['runtime_seconds']['anydoc']:<7.2f}"
    )

    print()
    print("TEXT QUALITY AND STRUCTURE   (c = current, a = anydoc)")
    print("-" * 92)
    print(
        f"{'category':<24}{'words c/a':>15}{'broken c/a':>15}{'fused c/a':>15}"
        f"{'years c/a':>11}{'ISIN c/a':>10}"
    )

    for document in documents:
        current = document["parsers"]["current"]

        anydoc = document["parsers"]["anydoc"]

        def value(node: dict[str, Any], *path: str) -> Any:
            result: Any = node

            for key in path:
                result = (result or {}).get(key) if isinstance(result, dict) else None

            return result if result is not None else 0

        print(
            f"{document['category']:<24}"
            f"{value(current, 'text', 'word_count'):>7}/"
            f"{value(anydoc, 'text', 'word_count'):<7}"
            f"{value(current, 'text', 'broken_sentence_ratio'):>7.2f}/"
            f"{value(anydoc, 'text', 'broken_sentence_ratio'):<7.2f}"
            f"{value(current, 'text', 'glued_token_ratio'):>7.3f}/"
            f"{value(anydoc, 'text', 'glued_token_ratio'):<7.3f}"
            f"{value(current, 'text', 'distinct_years'):>5}/"
            f"{value(anydoc, 'text', 'distinct_years'):<5}"
            f"{value(current, 'text', 'distinct_isins'):>4}/"
            f"{value(anydoc, 'text', 'distinct_isins'):<4}"
        )

    print()
    print("PER DOCUMENT")
    print("-" * 92)

    for document in documents:
        verdict = document["verdict"]

        print()
        print(f"[{document['category']}] source {document['source_id']} - {document['url'][:70]}")
        print(f"  chosen because : {document['why_chosen']}")
        print(
            f"  better parser  : {verdict['winner']} (score {verdict['score']:+d})"
            f"   -> {verdict['recommended_role']}"
        )

        for reason in verdict["reasons"]:
            print(f"    - {reason}")

        if verdict["missed_by_current"]:
            print(f"  current missed : {'; '.join(verdict['missed_by_current'])}")

        if verdict["missed_by_anydoc"]:
            print(f"  anydoc missed  : {'; '.join(verdict['missed_by_anydoc'])}")

    print()
    print("WINNERS   : " + ", ".join(f"{key}={value}" for key, value in totals["winners"].items()))
    print(
        "ROLES     : "
        + ", ".join(f"{key}={value}" for key, value in totals["recommended_roles"].items())
    )
    print(
        f"OUTPUT    : current {totals['output_bytes']['current'] / 1024:.0f} KiB, "
        f"anydoc {totals['output_bytes']['anydoc'] / 1024:.0f} KiB"
    )

    recommendation = report.get("recommendation")

    if not recommendation:
        return

    print()
    print("=" * 92)
    print(f"RECOMMENDATION: {recommendation['role']}")
    print("=" * 92)
    print(recommendation["summary"])

    print()
    print("Use AnyDoc for:")

    for line in recommendation["use_it_for"]:
        print(f"  + {line}")

    print()
    print("Do not use AnyDoc for:")

    for line in recommendation["do_not_use_it_for"]:
        print(f"  - {line}")

    print()
    print("Blockers before it could be promoted:")

    for line in recommendation["blockers"]:
        print(f"  ! {line}")

    print()
    print(
        f"Replace the Step 6 parser: {'yes' if recommendation['replace_current_parser'] else 'no'}"
    )


if __name__ == "__main__":
    sys.exit(main())
