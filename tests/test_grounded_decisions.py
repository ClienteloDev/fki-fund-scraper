from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from fundscraper.grounded_decisions import (
    GroundedDecision,
    GroundedDecisionFile,
    GroundedDecisionStatus,
)
from fundscraper.output_models import (
    Confidence,
)


def test_validates_found_grounded_decision() -> None:
    decision_file = GroundedDecisionFile(
        provider="manual-review",
        generated_at=datetime.now(UTC),
        decisions=[
            GroundedDecision(
                packet_id=("fund_0123456789abcdef:target_return"),
                status=(GroundedDecisionStatus.FOUND),
                value={"value_percent_pa": 8},
                snippet_ids=["1:3:1"],
                confidence=Confidence.HIGH,
            )
        ],
    )

    assert len(decision_file.decisions) == 1

    decision = decision_file.decisions[0]

    assert decision.status is GroundedDecisionStatus.FOUND

    assert decision.confidence is Confidence.HIGH

    assert decision.value == {"value_percent_pa": 8}

    assert decision.snippet_ids == ["1:3:1"]


def test_rejects_found_decision_without_snippet() -> None:
    with pytest.raises(
        ValidationError,
        match="requires at least one snippet",
    ):
        GroundedDecision(
            packet_id=("fund_0123456789abcdef:target_return"),
            status=(GroundedDecisionStatus.FOUND),
            value={"value_percent_pa": 8},
            snippet_ids=[],
        )


def test_rejects_found_decision_without_value() -> None:
    with pytest.raises(
        ValidationError,
        match="requires a value",
    ):
        GroundedDecision(
            packet_id=("fund_0123456789abcdef:target_return"),
            status=(GroundedDecisionStatus.FOUND),
            value=None,
            snippet_ids=["1:3:1"],
        )


def test_rejects_non_found_decision_with_value() -> None:
    with pytest.raises(
        ValidationError,
        match="must not contain a value",
    ):
        GroundedDecision(
            packet_id=("fund_0123456789abcdef:target_return"),
            status=(GroundedDecisionStatus.NOT_FOUND),
            value={"value_percent_pa": 8},
            snippet_ids=["1:3:1"],
            reason_detail=("The supplied evidence does not establish a reliable target return."),
        )


def test_rejects_non_found_decision_without_reason() -> None:
    with pytest.raises(
        ValidationError,
        match="requires reason_detail",
    ):
        GroundedDecision(
            packet_id=("fund_0123456789abcdef:target_return"),
            status=(GroundedDecisionStatus.NOT_FOUND),
            value=None,
            snippet_ids=[],
            reason_detail=None,
        )


def test_rejects_duplicate_packet_ids() -> None:
    packet_id = "fund_0123456789abcdef:target_return"

    first_decision = GroundedDecision(
        packet_id=packet_id,
        status=(GroundedDecisionStatus.NOT_FOUND),
        reason_detail=("The target return was not quantified."),
    )

    second_decision = GroundedDecision(
        packet_id=packet_id,
        status=(GroundedDecisionStatus.AMBIGUOUS),
        reason_detail=("The supplied text does not clearly refer to the exact fund."),
    )

    with pytest.raises(
        ValidationError,
        match="duplicate packet_id",
    ):
        GroundedDecisionFile(
            provider="manual-review",
            generated_at=datetime.now(UTC),
            decisions=[
                first_decision,
                second_decision,
            ],
        )
