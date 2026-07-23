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
from fundscraper.fallback_sources import (
    FallbackField,
)
from fundscraper.field_extraction import (
    extract_fund_fields,
)
from fundscraper.grounding_packets import (
    build_grounding_packets,
    write_grounding_packets,
)
from fundscraper.models import FundInput
from fundscraper.output_models import (
    ProcessingMetadata,
    ProcessingStatus,
)
from fundscraper.output_service import (
    create_pending_output,
    stable_fund_id,
)


def test_builds_grounded_packet_for_unresolved_field(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example Fund SICAV a.s.",
        web="https://example.com/fund",
    )

    fund_id = stable_fund_id(fund)

    database_path = tmp_path / "fundscraper.sqlite3"

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
        url="https://example.com/memorandum.pdf",
        status=SourceStatus.DOWNLOADED,
        document_type="memorandum",
        content_type="application/pdf",
        title="Example Fund investment memorandum",
        retrieved_at=datetime(
            2026,
            7,
            23,
            12,
            0,
            tzinfo=UTC,
        ),
        http_status=200,
        sha256="a" * 64,
        local_path="cache/http/example.body",
    )

    text = """
    Example Fund SICAV a.s.

    Investicni strategie fondu je zamerena na dlouhodoby rust.

    Ocekavany vynos fondu se muze pohybovat kolem 8 % p.a.,
    ale tato hodnota neni garantovana.
    """.strip()

    parsed_document = ParsedDocument(
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

    outputs = create_pending_output(
        [
            fund,
        ]
    )

    missing_fields = extract_fund_fields(
        fund_name=fund.name,
        documents=[],
    )

    outputs[0] = outputs[0].model_copy(
        update={
            "investment_horizon": (missing_fields.investment_horizon),
            "minimum_investment": (missing_fields.minimum_investment),
            "target_return": (missing_fields.target_return),
            "fees": missing_fields.fees,
            "assets_under_management": (missing_fields.assets_under_management),
            "processing": ProcessingMetadata(
                status=ProcessingStatus.PARTIAL,
                updated_at=datetime.now(UTC),
            ),
        }
    )

    report = build_grounding_packets(
        funds=[
            fund,
        ],
        outputs=outputs,
        database_path=database_path,
        max_snippets=5,
    )

    assert report.funds_considered == 1
    assert report.funds_with_packets == 1
    assert report.packets_total == 5
    assert report.packets_with_context == 1
    assert report.packets_without_context == 4

    target_packet = next(
        packet for packet in report.packets if (packet.field is FallbackField.TARGET_RETURN)
    )

    assert target_packet.insufficient_context is False

    assert len(target_packet.snippets) == 1

    snippet = target_packet.snippets[0]

    assert snippet.page == 3

    assert "Ocekavany vynos" in snippet.quote

    assert snippet.scope_warning is False

    assert snippet.fund_identity_matches >= 1

    report_path = tmp_path / "grounding-packets.json"

    write_grounding_packets(
        report=report,
        path=report_path,
    )

    assert report_path.exists()
