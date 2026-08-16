"""The official ISIN register reaching a real extraction run.

`official_isin_scope` was already unit-tested. What these tests cover is the wiring: that an index
handed to the extraction run actually decides identity inside `extract_fund_fields` and
`extract_extended_fields`, and that leaving it out changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import DocumentFormat, ParsedDocument, ParsedPage
from fundscraper.field_extraction import (
    ExtractionDocument,
    IsinIdentity,
    SourceScope,
    active_isin_identity,
    extract_fund_fields,
    official_isin_identity,
)

FUND = "QH Letting SICAV a.s."
OTHER = "Compact Property Fund, a.s."
ISIN = "CZ1005201556"
OTHER_ISIN = "CZ0008041365"

# A KID that prices one class: it prints the class ISIN and the horizon, and never repeats the
# fund's legal name. Name-based identity refuses it; the register does not.
KID = (
    "Sdělení klíčových informací. Fond kvalifikovaných investorů. "
    f"ISIN {ISIN}. "
    "Typický investor by měl být schopen investici ve fondu držet po dobu nejméně 7 let. "
    "Doporučená doba držení: 7 let."
)

INDEX = {
    ISIN: IsinIdentity(fund_name=FUND, scope=SourceScope.EXACT_FUND),
    OTHER_ISIN: IsinIdentity(fund_name=OTHER, scope=SourceScope.EXACT_FUND),
}


def _document(text: str) -> ExtractionDocument:
    page = ParsedPage(page_number=1, text=text, character_count=len(text))

    parsed = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="test",
        pages=(page,),
        character_count=len(text),
        scanned_candidate=False,
    )

    record = ParsedDocumentRecord(
        source_id=1,
        fund_id="test-fund",
        url="https://www.amista.cz/files/qhlscv/statut.pdf",
        title=None,
        document_type="priips_kid",
        content_type="application/pdf",
        retrieved_at=datetime.now(UTC).isoformat(),
        document_format=DocumentFormat.PDF,
        parser_name="test",
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path="unused.json",
        parsed_at=datetime.now(UTC).isoformat(),
    )

    return ExtractionDocument(record=record, document=parsed)


def test_the_register_lets_a_real_extraction_read_a_document_that_never_names_the_fund() -> None:
    documents = [_document(KID)]

    without = extract_fund_fields(fund_name=FUND, documents=documents)

    with official_isin_identity(INDEX):
        with_register = extract_fund_fields(fund_name=FUND, documents=documents)

    assert without.investment_horizon.value is None
    assert with_register.investment_horizon.value is not None
    assert with_register.investment_horizon.value.minimum_years == 7.0


def test_an_isin_of_another_fund_is_still_refused_inside_a_real_run() -> None:
    documents = [_document(KID.replace(ISIN, OTHER_ISIN))]

    with official_isin_identity(INDEX):
        result = extract_fund_fields(fund_name=FUND, documents=documents)

    assert result.investment_horizon.value is None


def test_the_register_does_not_leak_out_of_its_run() -> None:
    assert active_isin_identity() is None

    with official_isin_identity(INDEX):
        assert active_isin_identity() == INDEX

    assert active_isin_identity() is None


def test_an_empty_register_behaves_exactly_like_no_register() -> None:
    documents = [_document(KID)]

    baseline = extract_fund_fields(fund_name=FUND, documents=documents)

    with official_isin_identity({}):
        empty = extract_fund_fields(fund_name=FUND, documents=documents)

    assert empty.investment_horizon.status == baseline.investment_horizon.status
    assert empty.investment_horizon.value == baseline.investment_horizon.value
