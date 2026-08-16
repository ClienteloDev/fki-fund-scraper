from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fundscraper.database import (
    SourceStatus,
    initialize_database,
    record_parsed_document,
    register_funds,
    upsert_source,
)
from fundscraper.document_parser import (
    DocumentFormat,
    ParsedDocument,
    ParsedPage,
    write_parsed_document,
)
from fundscraper.extraction_service import (
    extract_fund_data,
)
from fundscraper.models import FundInput
from fundscraper.output_models import (
    FieldStatus,
)
from fundscraper.output_service import (
    create_pending_output,
    load_output,
    stable_fund_id,
    write_output,
)


def test_extracts_and_updates_output_file(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com",
    )

    fund_id = stable_fund_id(fund)

    database_path = tmp_path / "fundscraper.sqlite3"

    output_path = tmp_path / "funds.enriched.json"

    parsed_directory = tmp_path / "parsed"

    initialize_database(database_path)

    register_funds(
        database_path,
        [
            fund,
        ],
    )

    source_id = upsert_source(
        database_path,
        fund_id=fund_id,
        url="https://example.com/kid.pdf",
        status=SourceStatus.DOWNLOADED,
        document_type="priips_kid",
        content_type="application/pdf",
        title="KID",
        retrieved_at=datetime.now(UTC),
        http_status=200,
        sha256="a" * 64,
        local_path="cache/http/example.body",
    )

    # The KID names the fund it prices. The host of the URL is not allowed
    # to stand in for that, so the document has to carry its own identity.
    text = """
    Example SICAV a.s.
    Sdělení klíčových informací
    Doporučený investiční horizont je 5 let.
    Minimální investice činí 1 000 000 Kč.
    Cílový výnos je 8 % p.a.
    Vstupní poplatek činí 2 %.
    Hodnota majetku fondu k 31. 12. 2025 činila 500 mil. Kč.
    """.strip()

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

    parsed_path = write_parsed_document(
        directory=parsed_directory,
        fund_id=fund_id,
        source_id=source_id,
        document=parsed_document,
    )

    record_parsed_document(
        database_path,
        source_id=source_id,
        fund_id=fund_id,
        document_format="pdf",
        parser_name="pymupdf",
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path=str(parsed_path),
    )

    write_output(
        output_path,
        create_pending_output(
            [
                fund,
            ]
        ),
    )

    summary = extract_fund_data(
        database_path=database_path,
        output_path=output_path,
        fund=fund,
    )

    output = load_output(output_path)

    assert summary.fields_found == 5
    assert summary.fields_missing == 0

    assert len(output) == 1

    assert output[0].investment_horizon.status is FieldStatus.FOUND

    assert output[0].minimum_investment.status is FieldStatus.FOUND

    assert output[0].target_return.status is FieldStatus.FOUND

    assert output[0].fees.status is FieldStatus.FOUND

    assert output[0].assets_under_management.status is FieldStatus.FOUND
