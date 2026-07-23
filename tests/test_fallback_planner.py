from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import HttpUrl

from fundscraper.database import (
    SourceStatus,
    initialize_database,
    record_parsed_document,
    register_funds,
    upsert_source,
)
from fundscraper.fallback_planner import (
    FallbackActionType,
    build_fallback_plan,
)
from fundscraper.fallback_sources import (
    ApprovedFallbackSource,
    FallbackField,
    FallbackSourceType,
)
from fundscraper.field_extraction import (
    extract_fund_fields,
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


def test_builds_ordered_fallback_actions(
    tmp_path: Path,
) -> None:
    first_fund = FundInput(
        name="First Fund SICAV a.s.",
        web="https://manager.example.com/first",
    )

    second_fund = FundInput(
        name="Second Fund SICAV a.s.",
        web="https://manager.example.com/second",
    )

    funds = [
        first_fund,
        second_fund,
    ]

    database_path = tmp_path / "fundscraper.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        funds,
    )

    fund_id = stable_fund_id(first_fund)

    source_id = upsert_source(
        database_path,
        fund_id=fund_id,
        url="https://manager.example.com/scanned.pdf",
        status=SourceStatus.DOWNLOADED,
        document_type="statute",
        content_type="application/pdf",
        retrieved_at=datetime.now(UTC),
        http_status=200,
        sha256="a" * 64,
        local_path="cache/http/scanned.body",
    )

    record_parsed_document(
        database_path,
        source_id=source_id,
        fund_id=fund_id,
        document_format="pdf",
        parser_name="pymupdf",
        page_count=4,
        character_count=0,
        scanned_candidate=True,
        text_path="cache/parsed/scanned.json",
    )

    outputs = create_pending_output(funds)

    missing_fields = extract_fund_fields(
        fund_name=first_fund.name,
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

    approved_source = ApprovedFallbackSource(
        name="Approved Database",
        base_url=HttpUrl("https://database.example.com"),
        source_type=(FallbackSourceType.SECONDARY_DATABASE),
        priority=100,
        supported_fields=[FallbackField.MINIMUM_INVESTMENT],
    )

    report = build_fallback_plan(
        funds=funds,
        outputs=outputs,
        database_path=database_path,
        approved_sources=[approved_source],
    )

    assert report.funds_considered == 2

    assert report.funds_requiring_fallback == 1

    plan = report.plans[0]

    assert len(plan.missing_fields) == 5

    action_types = {action.action for action in plan.actions}

    assert FallbackActionType.EXPANDED_CRAWL in action_types

    assert FallbackActionType.DOMAIN_ADAPTER in action_types

    assert FallbackActionType.OCR in action_types

    assert FallbackActionType.APPROVED_SOURCE in action_types

    assert FallbackActionType.GROUNDED_LLM in action_types

    assert FallbackActionType.MANUAL_REVIEW in action_types
