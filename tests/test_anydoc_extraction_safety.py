"""
The two rules that keep a fallback reading from delivering a wrong value.

A converter that merges a whole cost table into one row leaves every fee
label and one percentage on the same line, and a value quoted from a
passage that could not be placed on a page cannot be checked against the
document it came from.
"""

from __future__ import annotations

from fundscraper.anydoc_fallback import ANYDOC_FALLBACK_PARSER_NAME
from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentFormat,
    ParsedDocument,
    ParsedPage,
)
from fundscraper.extended_extraction import extract_extended_fields
from fundscraper.field_extraction import (
    ExtractionDocument,
    extract_fees,
    extract_fund_fields,
    is_collapsed_fee_line,
)
from fundscraper.output_models import (
    Confidence,
    FeeFrequency,
    FeeItem,
    FeeType,
    FieldResult,
    FieldStatus,
    InvestmentHorizonValue,
)

FUND_NAME = "Example Fund SICAV a.s."


def fee_item(
    fee_type: FeeType,
    rate: float | None,
) -> FeeItem:
    return FeeItem(
        type=fee_type,
        rate_percent=rate,
        frequency=FeeFrequency.ANNUAL,
    )


def extraction_document(
    *,
    text: str,
    page_number: int | None,
    parser_name: str,
) -> ExtractionDocument:
    document = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name=parser_name,
        pages=(
            ParsedPage(
                page_number=page_number,
                text=text,
                character_count=len(text),
            ),
        ),
        character_count=len(text),
        scanned_candidate=False,
    )

    record = ParsedDocumentRecord(
        source_id=1,
        fund_id="fund_0123456789abcdef",
        url="https://example.com/kid.pdf",
        title="Example Fund SICAV a.s. key information",
        document_type="memorandum",
        content_type="application/pdf",
        retrieved_at="2026-07-23T12:00:00+00:00",
        document_format="pdf",
        parser_name=parser_name,
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path="cache/parsed/1.json",
        parsed_at="2026-07-23T12:01:00+00:00",
    )

    return ExtractionDocument(
        record=record,
        document=document,
    )


# ---------------------------------------------------------------------------
# One collapsed row must not become several fees
# ---------------------------------------------------------------------------


def test_one_rate_shared_by_several_fees_is_a_collapsed_row() -> None:
    assert is_collapsed_fee_line(
        {
            FeeType.ENTRY: fee_item(FeeType.ENTRY, 3.0),
            FeeType.EXIT: fee_item(FeeType.EXIT, 3.0),
            FeeType.ONGOING: fee_item(FeeType.ONGOING, 3.0),
        }
    )


def test_different_rates_on_one_line_are_kept() -> None:
    assert not is_collapsed_fee_line(
        {
            FeeType.ENTRY: fee_item(FeeType.ENTRY, 3.0),
            FeeType.EXIT: fee_item(FeeType.EXIT, 1.0),
        }
    )


def test_a_single_fee_on_a_line_is_never_collapsed() -> None:
    assert not is_collapsed_fee_line({FeeType.ENTRY: fee_item(FeeType.ENTRY, 3.0)})


def test_a_table_of_zeroes_is_left_alone() -> None:
    # A cost table really does state that several fees are not charged.
    assert not is_collapsed_fee_line(
        {
            FeeType.EXIT: fee_item(FeeType.EXIT, 0.0),
            FeeType.ONGOING: fee_item(FeeType.ONGOING, 0.0),
        }
    )


def test_a_collapsed_cost_table_yields_no_fees() -> None:
    # This is the shape a merged key information document produces: every
    # label and one rate on a single line.
    collapsed = (
        "Example Fund SICAV a.s.\n"
        "Naklady, pokud investici ukoncite po uplynuti jednoho roku "
        "Maximalne 3 % z castky Jednorazove naklady Naklady na vstup 3,00 % 300 EUR "
        "Naklady na vystup 0,00 % Vystupni poplatek neni stanoven "
        "Prubezne naklady uctovane kazdy rok Vykonnostni poplatky 0,00 %"
    )

    result = extract_fees(
        fund_name=FUND_NAME,
        documents=[
            extraction_document(
                text=collapsed,
                page_number=1,
                parser_name=ANYDOC_FALLBACK_PARSER_NAME,
            )
        ],
    )

    assert result.status is not FieldStatus.FOUND


def test_fees_stated_on_their_own_lines_are_still_read() -> None:
    stated = (
        "Example Fund SICAV a.s.\n"
        "Vstupni poplatek cini maximalne 3 %.\n"
        "Poplatek za obhospodarovani cini 1,5 % rocne.\n"
        "Vykonnostni poplatek cini 20 %."
    )

    result = extract_fees(
        fund_name=FUND_NAME,
        documents=[
            extraction_document(
                text=stated,
                page_number=1,
                parser_name="pymupdf",
            )
        ],
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    rates = sorted(
        item.rate_percent for item in result.value.items if item.rate_percent is not None
    )

    assert rates == [1.5, 3.0, 20.0]


# ---------------------------------------------------------------------------
# A quote without a page is never delivered as certain
# ---------------------------------------------------------------------------


HORIZON_TEXT = (
    "Example Fund SICAV a.s.\nDoporuceny investicni horizont je 5 let.\nFond investuje dlouhodobe."
)


def horizon_result(
    *,
    parser_name: str,
    page_number: int | None,
) -> FieldResult[InvestmentHorizonValue]:
    return extract_fund_fields(
        fund_name=FUND_NAME,
        documents=[
            extraction_document(
                text=HORIZON_TEXT,
                page_number=page_number,
                parser_name=parser_name,
            )
        ],
    ).investment_horizon


def test_a_fallback_value_without_a_page_is_marked_for_review() -> None:
    result = horizon_result(
        parser_name=ANYDOC_FALLBACK_PARSER_NAME,
        page_number=None,
    )

    assert result.status is FieldStatus.FOUND

    assert result.extraction is not None

    assert result.extraction.review_required

    assert result.extraction.confidence is not Confidence.HIGH

    assert result.source is not None and result.source.page is None


def test_a_fallback_value_is_reviewed_even_when_it_has_a_page() -> None:
    # Rebuilding a page puts words next to each other that were never
    # adjacent on it, so a label can pick up a number belonging to a
    # different part of the page. Recovering the page number says where
    # to look, not that the pairing is right.
    placed = horizon_result(
        parser_name=ANYDOC_FALLBACK_PARSER_NAME,
        page_number=3,
    )

    primary = horizon_result(
        parser_name="pymupdf",
        page_number=3,
    )

    assert placed.status is FieldStatus.FOUND

    assert placed.extraction is not None and primary.extraction is not None

    assert placed.extraction.review_required

    assert not primary.extraction.review_required

    assert placed.extraction.confidence is not Confidence.HIGH

    assert placed.source is not None and placed.source.page == 3


def test_a_fallback_value_without_a_page_is_worth_least() -> None:
    unplaced = horizon_result(
        parser_name=ANYDOC_FALLBACK_PARSER_NAME,
        page_number=None,
    )

    assert unplaced.extraction is not None

    assert unplaced.extraction.confidence is Confidence.LOW


CAPITAL_TEXT = (
    "Example Fund SICAV a.s.\n"
    "Fondovy kapital k 31. 12. 2024 cinil 1 250 000 000 Kc.\n"
    "Fondovy kapital k 31. 12. 2023 cinil 900 000 000 Kc."
)


def test_a_series_read_from_the_fallback_is_reviewed_too() -> None:
    # The capital and performance series are assembled in their own
    # module, outside the result builder the delivered fields use. The
    # same doubt applies to them, and one of them carried a per-share
    # price into an assets figure before this was closed.
    fallback = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=None,
        documents=[
            extraction_document(
                text=CAPITAL_TEXT,
                page_number=2,
                parser_name=ANYDOC_FALLBACK_PARSER_NAME,
            )
        ],
    ).aum_history

    primary = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=None,
        documents=[
            extraction_document(
                text=CAPITAL_TEXT,
                page_number=2,
                parser_name="pymupdf",
            )
        ],
    ).aum_history

    assert fallback.status is FieldStatus.FOUND

    assert primary.status is FieldStatus.FOUND

    assert fallback.extraction is not None and primary.extraction is not None

    assert fallback.extraction.review_required

    assert fallback.extraction.confidence is not Confidence.HIGH


def test_the_primary_parser_is_not_affected_by_the_page_rule() -> None:
    # The markup parser reports no page numbers either, and its values
    # must keep the standing they have always had.
    result = horizon_result(
        parser_name="pymupdf",
        page_number=None,
    )

    assert result.status is FieldStatus.FOUND

    assert result.extraction is not None

    assert not result.extraction.review_required
