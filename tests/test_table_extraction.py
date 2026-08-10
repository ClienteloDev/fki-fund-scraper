from __future__ import annotations

from datetime import date

from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentFormat,
    DocumentTable,
    ParsedDocument,
    ParsedPage,
)
from fundscraper.extended_extraction import (
    collect_capital_candidates,
    collect_historical_candidates,
    collect_return_candidates,
)
from fundscraper.field_extraction import (
    ExtractionDocument,
    _fee_candidates_from_tables,
    normalized_document_lines,
)
from fundscraper.output_models import (
    AumMetricType,
    FeeType,
    HistoricalValueType,
    ReturnSeriesType,
)
from fundscraper.table_extraction import (
    column_periods,
    iter_labelled_rows,
    iter_table_values,
    leading_percentage,
)

# The capital table of a real Czech annual report: the dates stand in the
# header row and each figure sits in the column of its own period.
CAPITAL_TABLE = DocumentTable(
    rows=(
        ("", "k datu", "", "31.12.2024", "", "31.12.2023"),
        ("", "Fondový kapitál Podfondu (Kč)", "", "4 138 763 201", "", "858 036 978"),
        ("", "Fondový kapitál na 1 akcii (Kč)", "", "1,1446", "", "1,0549"),
    ),
)


FEE_TABLE = DocumentTable(
    rows=(
        ("Výstupní poplatek:", "30 % při odkupu investičních akcií do 3 let"),
        ("Vstupní poplatek:", "3 % z investované částky"),
    ),
)


RETURN_TABLE = DocumentTable(
    rows=(
        ("Rok", "2022", "2023", "2024"),
        ("Roční výkonnost fondu", "5,1 %", "7,7 %", "6,5 %"),
    ),
)


def _document(
    tables: tuple[DocumentTable, ...],
    *,
    text: str = "Rezidento Alfa SICAV, a.s.",
    document_type: str = "annual_report",
) -> list[ExtractionDocument]:
    page = ParsedPage(
        page_number=1,
        text=text,
        character_count=len(text),
        tables=tables,
    )

    parsed = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(page,),
        character_count=page.character_count,
        scanned_candidate=False,
    )

    record = ParsedDocumentRecord(
        source_id=1,
        fund_id="fund_0123456789abcdef",
        url="https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2024.pdf",
        title="Rezidento Alfa SICAV, a.s.",
        document_type=document_type,
        content_type="application/pdf",
        retrieved_at="2026-01-01T00:00:00+00:00",
        document_format="pdf",
        parser_name="pymupdf",
        page_count=1,
        character_count=page.character_count,
        scanned_candidate=False,
        text_path="",
        parsed_at="2026-01-01T00:00:00+00:00",
    )

    return [
        ExtractionDocument(
            record=record,
            document=parsed,
        )
    ]


# ---------------------------------------------------------------------------
# Reading a grid as a grid
# ---------------------------------------------------------------------------


def test_a_header_row_gives_each_column_its_own_period() -> None:
    dates, years = column_periods(CAPITAL_TABLE)

    assert dates == {
        3: date(2024, 12, 31),
        5: date(2023, 12, 31),
    }

    assert years == {}

    year_dates, year_columns = column_periods(RETURN_TABLE)

    assert year_dates == {}

    assert year_columns == {
        1: 2022,
        2: 2023,
        3: 2024,
    }


def test_every_figure_keeps_the_label_of_its_row_and_the_date_of_its_column() -> None:
    values = iter_table_values(CAPITAL_TABLE)

    capital = [item for item in values if "Podfondu" in item.label]

    assert [(item.value, item.as_of) for item in capital] == [
        (4_138_763_201.0, date(2024, 12, 31)),
        (858_036_978.0, date(2023, 12, 31)),
    ]

    # The currency is written once, in the label of the row.
    assert {item.currency for item in capital} == {"CZK"}


def test_a_rate_sharing_its_cell_with_a_condition_is_still_read() -> None:
    rows = iter_labelled_rows(FEE_TABLE)

    assert rows[0].label == "Výstupní poplatek:"

    assert leading_percentage(rows[0].cells[0]) == 30.0

    assert leading_percentage("bez poplatku") is None


# ---------------------------------------------------------------------------
# What the grid prevents
# ---------------------------------------------------------------------------


def test_the_grid_dates_each_capital_figure_with_its_own_column() -> None:
    """
    Flattened, the row reads "... 4 138 763 201 858 036 978" and both
    figures fall under whichever date the text extractor reaches first.
    Read as a grid each one keeps the date of the column it sits in.
    """

    candidates = collect_capital_candidates(_document((CAPITAL_TABLE,)))

    dated = {
        (
            candidate.observation.amount,
            candidate.observation.as_of,
        )
        for candidate in candidates
    }

    assert (4_138_763_201.0, date(2024, 12, 31)) in dated

    assert (858_036_978.0, date(2023, 12, 31)) in dated

    # The older figure must not inherit the newer date.
    assert (858_036_978.0, date(2024, 12, 31)) not in dated


def test_a_per_share_figure_never_becomes_the_capital_of_the_fund() -> None:
    """
    "Fondovy kapital na 1 akcii" carries the capital wording but measures
    one share. Read as capital it reported a billion-crown fund as
    holding one crown.
    """

    capital = collect_capital_candidates(_document((CAPITAL_TABLE,)))

    assert all(candidate.observation.amount > 1_000 for candidate in capital)

    assert all(
        candidate.observation.metric_type is not AumMetricType.REGISTERED_CAPITAL
        for candidate in capital
    )

    historical = collect_historical_candidates(_document((CAPITAL_TABLE,)))

    per_share = [
        candidate
        for candidate in historical
        if candidate.value_type is HistoricalValueType.NAV_PER_SHARE
    ]

    assert {candidate.observation.value for candidate in per_share} == {
        1.1446,
        1.0549,
    }


def test_a_year_header_keeps_every_result_with_its_own_year() -> None:
    candidates = collect_return_candidates(_document((RETURN_TABLE,)))

    calendar = {
        (
            candidate.observation.year,
            candidate.observation.return_percent,
        )
        for candidate in candidates
        if candidate.observation.series_type is ReturnSeriesType.CALENDAR_YEAR
    }

    assert calendar == {
        (2022, 5.1),
        (2023, 7.7),
        (2024, 6.5),
    }


def test_a_fee_rate_is_taken_from_the_row_of_its_own_label() -> None:
    documents = _document(
        (FEE_TABLE,),
        document_type="price_list",
    )

    normalized_document, _ = normalized_document_lines(documents[0])

    candidates = _fee_candidates_from_tables(
        document=documents[0],
        normalized_document=normalized_document,
    )

    assert candidates

    rates = {item.type: item.rate_percent for item in candidates[0].value.items}

    assert rates[FeeType.EXIT] == 30.0

    assert rates[FeeType.ENTRY] == 3.0


def test_a_table_candidate_outranks_the_same_value_read_from_text() -> None:
    """A structured candidate carries its own label, date and currency."""

    text = "Fondový kapitál Podfondu k 31. 12. 2024 činil 4 138 763 201 Kč."

    documents = _document(
        (CAPITAL_TABLE,),
        text=f"Rezidento Alfa SICAV, a.s.\n{text}",
    )

    candidates = collect_capital_candidates(documents)

    from_table = [
        candidate for candidate in candidates if "Fondový kapitál Podfondu (Kč)" in candidate.quote
    ]

    from_text = [candidate for candidate in candidates if candidate not in from_table]

    assert from_table

    if from_text:
        assert max(item.score for item in from_table) > max(item.score for item in from_text)


def test_a_table_without_usable_structure_produces_nothing() -> None:
    """The text extractor stays responsible for what a grid cannot say."""

    undated = DocumentTable(
        rows=(
            ("Popis", "Hodnota"),
            ("Fondový kapitál", "4 138 763 201"),
        ),
    )

    # No date column and no currency, so the grid cannot date or
    # denominate the figure and refuses to guess.
    assert not collect_capital_candidates(_document((undated,)))
