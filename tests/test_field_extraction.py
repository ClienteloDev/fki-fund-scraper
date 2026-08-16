from __future__ import annotations

from fundscraper.database import (
    ParsedDocumentRecord,
)
from fundscraper.document_parser import (
    DocumentFormat,
    ParsedDocument,
    ParsedPage,
)
from fundscraper.field_extraction import (
    ExtractionDocument,
    extract_fund_fields,
)
from fundscraper.output_models import (
    AumMetricType,
    FieldStatus,
    ReasonCode,
)


def test_extracts_all_supported_fields() -> None:
    # The memorandum names its fund on its title page, as a real one does.
    # Without that the source proves no identity, and the host of the URL
    # is deliberately not allowed to stand in for it.
    text = """
    Example SICAV a.s.
    Investiční memorandum

    Doporučený investiční horizont je 5 let.

    Minimální investice činí 1 000 000 Kč.

    Cílový výnos fondu je 8 % p.a.

    Vstupní poplatek činí maximálně 3 %.
    Poplatek za obhospodařování činí 1,5 % ročně.
    Výkonnostní poplatek činí 20 %.

    Hodnota majetku fondu k 31. 12. 2025
    činila 2,5 mld. Kč.
    """.strip()

    parsed_document = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=4,
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
        url="https://example.com/memorandum.pdf",
        title="Investiční memorandum",
        document_type="memorandum",
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

    result = extract_fund_fields(
        fund_name="Example SICAV a.s.",
        documents=[
            ExtractionDocument(
                record=record,
                document=parsed_document,
            )
        ],
    )

    assert result.investment_horizon.status is FieldStatus.FOUND

    assert result.investment_horizon.value is not None

    assert result.investment_horizon.value.recommended_years == 5

    assert result.minimum_investment.status is FieldStatus.FOUND

    assert result.minimum_investment.value is not None

    assert result.minimum_investment.value.amount == 1_000_000

    assert result.minimum_investment.value.currency == "CZK"

    assert result.target_return.status is FieldStatus.FOUND

    assert result.target_return.value is not None

    assert result.target_return.value.value_percent_pa == 8

    assert result.fees.status is FieldStatus.FOUND
    assert result.fees.value is not None
    assert len(result.fees.value.items) == 3

    assert result.assets_under_management.status is FieldStatus.FOUND

    assert result.assets_under_management.value is not None

    assert result.assets_under_management.value.amount == 2_500_000_000

    # "Hodnota majetku fondu" is a labelled total, so the shared capital
    # vocabulary names it. It used to fall through to the unclassified
    # fund_aum because the assets extractor carried its own shorter
    # ladder; both are fund-level and deliverable, and the named one
    # tells a reader which line of the statement was read.
    assert result.assets_under_management.value.metric_type is AumMetricType.ASSETS_TOTAL

    assert result.assets_under_management.value.as_of.isoformat() == "2025-12-31"


def test_returns_structured_missing_results() -> None:
    result = extract_fund_fields(
        fund_name="Empty Fund",
        documents=[],
    )

    assert result.investment_horizon.status is FieldStatus.NOT_FOUND

    assert result.minimum_investment.status is FieldStatus.NOT_FOUND

    assert result.target_return.status is FieldStatus.NOT_FOUND

    assert result.fees.status is FieldStatus.NOT_FOUND

    assert result.assets_under_management.status is FieldStatus.NOT_FOUND


def create_extraction_document(
    *,
    source_id: int,
    url: str,
    title: str,
    text: str,
    document_type: str = "memorandum",
) -> ExtractionDocument:
    parsed_document = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=1,
                text=text,
                character_count=len(text),
            ),
        ),
        character_count=len(text),
        scanned_candidate=False,
    )

    record = ParsedDocumentRecord(
        source_id=source_id,
        fund_id="fund_0123456789abcdef",
        url=url,
        title=title,
        document_type=document_type,
        content_type="application/pdf",
        retrieved_at="2026-07-23T12:00:00+00:00",
        document_format="pdf",
        parser_name="pymupdf",
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path=f"cache/parsed/{source_id}.json",
        parsed_at="2026-07-23T12:01:00+00:00",
    )

    return ExtractionDocument(
        record=record,
        document=parsed_document,
    )


def test_marks_conflicting_target_returns() -> None:
    """Two sources of the same standing that disagree stay conflicting."""

    first_document = create_extraction_document(
        source_id=1,
        url="https://example.com/memorandum.pdf",
        title="Example Fund memorandum",
        text=("Example Fund SICAV a.s.\nCilovy vynos fondu je 8 % p.a."),
    )

    second_document = create_extraction_document(
        source_id=2,
        url="https://example.com/memorandum-2.pdf",
        title="Example Fund memorandum",
        text=("Example Fund SICAV a.s.\nCilovy vynos fondu je 12 % p.a."),
    )

    result = extract_fund_fields(
        fund_name="Example Fund SICAV a.s.",
        documents=[
            first_document,
            second_document,
        ],
    )

    assert result.target_return.status is FieldStatus.CONFLICTING

    assert result.target_return.reason is not None

    assert result.target_return.reason.code is ReasonCode.CONFLICTING_VALUES

    assert len(result.target_return.attempted_sources) == 2


def test_prefers_the_stronger_document_type_and_keeps_the_other() -> None:
    """
    A memorandum states the target of a fund; a factsheet reports it.

    Step 8 decides between them instead of losing both, and the value it
    did not take stays in the delivered field as an attempted source.
    """

    memorandum = create_extraction_document(
        source_id=1,
        url="https://example.com/memorandum.pdf",
        title="Example Fund memorandum",
        text=("Example Fund SICAV a.s.\nCilovy vynos fondu je 8 % p.a."),
    )

    factsheet = create_extraction_document(
        source_id=2,
        url="https://example.com/factsheet.pdf",
        title="Example Fund factsheet",
        text=("Example Fund SICAV a.s.\nCilovy vynos fondu je 12 % p.a."),
        document_type="factsheet",
    )

    result = extract_fund_fields(
        fund_name="Example Fund SICAV a.s.",
        documents=[
            memorandum,
            factsheet,
        ],
    )

    assert result.target_return.status is FieldStatus.FOUND

    assert result.target_return.value is not None

    assert result.target_return.value.value_percent_pa == 8.0

    losing = [
        attempt
        for attempt in result.target_return.attempted_sources
        if attempt.outcome is ReasonCode.CONFLICTING_VALUES
    ]

    assert len(losing) == 1

    assert str(losing[0].url) == "https://example.com/factsheet.pdf"

    assert losing[0].detail is not None

    assert "document_type" in losing[0].detail


def test_rejects_manager_level_value() -> None:
    document = create_extraction_document(
        source_id=1,
        url="https://manager.example.com/products",
        title="Manager product overview",
        text=("Skupina spravuje vsechny fondy.\nMinimalni investice cini 1 000 000 Kc."),
        document_type="marketing_page",
    )

    result = extract_fund_fields(
        fund_name="Example Fund SICAV a.s.",
        documents=[
            document,
        ],
    )

    assert result.minimum_investment.status is FieldStatus.AMBIGUOUS

    assert result.minimum_investment.reason is not None

    assert result.minimum_investment.reason.code is ReasonCode.SCOPE_MISMATCH


def test_accepts_same_value_from_multiple_sources() -> None:
    first_document = create_extraction_document(
        source_id=1,
        url="https://example.com/kid.pdf",
        title="Example Fund KID",
        text=("Example Fund SICAV a.s.\nDoporuceny investicni horizont je 5 let."),
        document_type="priips_kid",
    )

    second_document = create_extraction_document(
        source_id=2,
        url="https://example.com/statute.pdf",
        title="Example Fund statute",
        text=("Example Fund SICAV a.s.\nInvesticni horizont je 5 let."),
        document_type="statute",
    )

    result = extract_fund_fields(
        fund_name="Example Fund SICAV a.s.",
        documents=[
            first_document,
            second_document,
        ],
    )

    assert result.investment_horizon.status is FieldStatus.FOUND

    assert result.investment_horizon.value is not None

    assert result.investment_horizon.value.recommended_years == 5
