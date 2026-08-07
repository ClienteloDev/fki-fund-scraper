from __future__ import annotations

from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentFormat,
    ParsedDocument,
    ParsedPage,
)
from fundscraper.field_extraction import (
    ExtractionDocument,
    extract_fees,
    extract_target_return,
)
from fundscraper.output_models import (
    Annualization,
    FeeTierBasis,
    FeeType,
    FieldStatus,
    ReasonCode,
    ReturnType,
)

FUND_NAME = "Rezidento Alfa SICAV, a.s."


def _documents(
    text: str,
    *,
    document_type: str = "statute",
) -> list[ExtractionDocument]:
    parsed = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=3,
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
        url="https://www.rezidentoalfa.cz/dokumenty/statut.pdf",
        title="Rezidento Alfa SICAV, a.s. - statut",
        document_type=document_type,
        content_type="application/pdf",
        retrieved_at="2026-07-23T12:00:00+00:00",
        document_format="pdf",
        parser_name="pymupdf",
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path="cache/parsed/example.json",
        parsed_at="2026-07-23T12:01:00+00:00",
    )

    return [
        ExtractionDocument(
            record=record,
            document=parsed,
        )
    ]


def test_marks_a_preferred_return_as_such() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Přednostní výnos prioritních investičních akcií činí 7 % p.a.
    """.strip()

    result = extract_target_return(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    assert result.value.value_percent_pa == 7

    assert result.value.return_type is ReturnType.PREFERRED

    assert result.value.annualization is Annualization.PER_ANNUM


def test_marks_a_guaranteed_minimum_as_such() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Minimální zhodnocení PIA činí 6,1 % p.a.
    """.strip()

    result = extract_target_return(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    assert result.value.return_type is ReturnType.GUARANTEED_MINIMUM

    assert result.value.share_class == "PIA"


def test_reads_a_hurdle_rate_written_without_a_return_word() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Výkonnostní odměna činí 20 % ze zisku nad hurdle rate 7 % ročně.
    """.strip()

    result = extract_target_return(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    assert result.value.return_type is ReturnType.HURDLE

    assert result.value.value_percent_pa == 7


def test_keeps_both_bounds_of_a_stated_range() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Očekávaný výnos fondu je 8 - 9 % p.a.
    """.strip()

    result = extract_target_return(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    assert result.value.minimum_percent_pa == 8

    assert result.value.maximum_percent_pa == 9

    assert result.value.return_type is ReturnType.EXPECTED


def test_reports_that_no_target_return_is_published() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Cílový výnos není stanoven a fond jej nezveřejňuje.
    """.strip()

    result = extract_target_return(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.NOT_FOUND

    assert result.reason is not None

    assert result.reason.code is ReasonCode.NOT_PUBLICLY_DISCLOSED

    assert result.attempted_sources


def test_reads_the_holding_period_tiers_of_an_exit_fee() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Výstupní poplatek do 1 roku 10 %, od 1 do 2 let 5 %, po 2 letech 0 %.
    """.strip()

    result = extract_fees(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    exit_fee = next(item for item in result.value.items if item.type is FeeType.EXIT)

    assert [
        (
            tier.from_months,
            tier.to_months,
            tier.rate_percent,
        )
        for tier in exit_fee.tiers
    ] == [
        (0, 12, 10.0),
        (12, 24, 5.0),
        (24, None, 0.0),
    ]

    assert all(tier.basis is FeeTierBasis.HOLDING_PERIOD for tier in exit_fee.tiers)


def test_keeps_both_bounds_of_a_fee_range() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Vstupní poplatek činí od 0 % do 6 % z výše investice.
    """.strip()

    result = extract_fees(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    entry_fee = next(item for item in result.value.items if item.type is FeeType.ENTRY)

    assert entry_fee.rate_percent == 6

    assert entry_fee.minimum_rate_percent == 0

    assert entry_fee.maximum_rate_percent == 6

    assert entry_fee.maximum is True


def test_keeps_a_negotiated_fee_that_publishes_no_number() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Vstupní poplatek se sjednává individuálně dle dohody s distributorem.
    """.strip()

    result = extract_fees(
        fund_name=FUND_NAME,
        documents=_documents(text),
    )

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    entry_fee = next(item for item in result.value.items if item.type is FeeType.ENTRY)

    assert entry_fee.rate_percent is None

    assert entry_fee.negotiable is True

    assert entry_fee.details is not None
