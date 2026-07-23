from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from fundscraper.fallback_sources import (
    FallbackField,
)
from fundscraper.grounded_application import (
    apply_grounded_decisions,
)
from fundscraper.grounded_decisions import (
    GroundedDecision,
    GroundedDecisionError,
    GroundedDecisionFile,
    GroundedDecisionStatus,
)
from fundscraper.grounding_packets import (
    FieldGroundingPacket,
    GroundingBatchReport,
    GroundingSnippet,
)
from fundscraper.models import FundInput
from fundscraper.output_models import (
    Confidence,
    DocumentType,
    ExtractionMethod,
    FieldStatus,
    ProcessingStatus,
)
from fundscraper.output_service import (
    create_pending_output,
    load_output,
    stable_fund_id,
    write_output,
)


def test_applies_grounded_target_return(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example Fund SICAV a.s.",
        web="https://example.com/fund",
    )

    fund_id = stable_fund_id(fund)

    output_path = tmp_path / "funds.enriched.json"

    write_output(
        output_path,
        create_pending_output(
            [
                fund,
            ]
        ),
    )

    snippet = GroundingSnippet(
        snippet_id="10:3:1",
        source_id=10,
        url=HttpUrl("https://example.com/memorandum.pdf"),
        title="Example Fund memorandum",
        document_type=(DocumentType.MEMORANDUM),
        retrieved_at=datetime(
            2026,
            7,
            23,
            12,
            0,
            tzinfo=UTC,
        ),
        page=3,
        quote=("Ocekavany vynos fondu je 8 % p.a."),
        score=200,
        matched_keywords=["ocekavany vynos"],
        fund_identity_matches=1,
        scope_warning=False,
    )

    packet = FieldGroundingPacket(
        packet_id=(f"{fund_id}:target_return"),
        fund_id=fund_id,
        fund_name=fund.name,
        web=HttpUrl(str(fund.web)),
        field=(FallbackField.TARGET_RETURN),
        current_status=(FieldStatus.NOT_FOUND),
        current_reason="not_quantified",
        generated_at=datetime.now(UTC),
        snippets=[snippet],
        insufficient_context=False,
        instructions=[],
        warnings=[],
    )

    packet_report = GroundingBatchReport(
        generated_at=datetime.now(UTC),
        funds_considered=1,
        funds_with_packets=1,
        packets_total=1,
        packets_with_context=1,
        packets_without_context=0,
        packets=[packet],
    )

    decisions = GroundedDecisionFile(
        provider="manual-review",
        generated_at=datetime.now(UTC),
        decisions=[
            GroundedDecision(
                packet_id=packet.packet_id,
                status=(GroundedDecisionStatus.FOUND),
                value={"value_percent_pa": 8},
                snippet_ids=[snippet.snippet_id],
                confidence=Confidence.HIGH,
            )
        ],
    )

    summary = apply_grounded_decisions(
        packet_report=packet_report,
        decision_file=decisions,
        output_path=output_path,
    )

    outputs = load_output(output_path)

    assert summary.decisions_received == 1

    assert summary.decisions_applied == 1

    assert summary.found_applied == 1

    assert summary.unresolved_applied == 0

    assert summary.funds_updated == 1

    result = outputs[0].target_return

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    assert result.value.value_percent_pa == 8

    assert result.source is not None

    assert result.source.page == 3

    assert str(result.source.source.url) == ("https://example.com/memorandum.pdf")

    assert result.extraction is not None

    assert result.extraction.method is ExtractionMethod.GROUNDED

    assert result.extraction.confidence is Confidence.HIGH

    assert result.extraction.review_required is False

    assert outputs[0].processing.status is ProcessingStatus.PARTIAL


def test_rejects_unknown_snippet_reference(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example Fund SICAV a.s.",
        web="https://example.com/fund",
    )

    fund_id = stable_fund_id(fund)

    output_path = tmp_path / "funds.enriched.json"

    write_output(
        output_path,
        create_pending_output(
            [
                fund,
            ]
        ),
    )

    snippet = GroundingSnippet(
        snippet_id="10:3:1",
        source_id=10,
        url=HttpUrl("https://example.com/memorandum.pdf"),
        title="Example Fund memorandum",
        document_type=(DocumentType.MEMORANDUM),
        retrieved_at=datetime.now(UTC),
        page=3,
        quote=("Ocekavany vynos fondu je 8 % p.a."),
        score=200,
        matched_keywords=["ocekavany vynos"],
        fund_identity_matches=1,
        scope_warning=False,
    )

    packet = FieldGroundingPacket(
        packet_id=(f"{fund_id}:target_return"),
        fund_id=fund_id,
        fund_name=fund.name,
        web=HttpUrl(str(fund.web)),
        field=(FallbackField.TARGET_RETURN),
        current_status=(FieldStatus.NOT_FOUND),
        current_reason="not_quantified",
        generated_at=datetime.now(UTC),
        snippets=[snippet],
        insufficient_context=False,
        instructions=[],
        warnings=[],
    )

    report = GroundingBatchReport(
        generated_at=datetime.now(UTC),
        funds_considered=1,
        funds_with_packets=1,
        packets_total=1,
        packets_with_context=1,
        packets_without_context=0,
        packets=[packet],
    )

    decisions = GroundedDecisionFile(
        provider="manual-review",
        generated_at=datetime.now(UTC),
        decisions=[
            GroundedDecision(
                packet_id=packet.packet_id,
                status=(GroundedDecisionStatus.FOUND),
                value={"value_percent_pa": 8},
                snippet_ids=["unknown-snippet"],
                confidence=Confidence.HIGH,
            )
        ],
    )

    with pytest.raises(
        GroundedDecisionError,
        match=("references snippets outside its packet"),
    ):
        apply_grounded_decisions(
            packet_report=report,
            decision_file=decisions,
            output_path=output_path,
        )


def test_rejects_unknown_packet(
    tmp_path: Path,
) -> None:
    fund = FundInput(
        name="Example Fund SICAV a.s.",
        web="https://example.com/fund",
    )

    output_path = tmp_path / "funds.enriched.json"

    write_output(
        output_path,
        create_pending_output(
            [
                fund,
            ]
        ),
    )

    report = GroundingBatchReport(
        generated_at=datetime.now(UTC),
        funds_considered=1,
        funds_with_packets=0,
        packets_total=0,
        packets_with_context=0,
        packets_without_context=0,
        packets=[],
    )

    decisions = GroundedDecisionFile(
        provider="manual-review",
        generated_at=datetime.now(UTC),
        decisions=[
            GroundedDecision(
                packet_id=("fund_unknown:target_return"),
                status=(GroundedDecisionStatus.NOT_FOUND),
                reason_detail=("The supplied evidence does not contain a target return."),
            )
        ],
    )

    with pytest.raises(
        GroundedDecisionError,
        match="unknown packet",
    ):
        apply_grounded_decisions(
            packet_report=report,
            decision_file=decisions,
            output_path=output_path,
        )
