from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fundscraper.models import FundInput
from fundscraper.output_models import FieldStatus
from fundscraper.output_service import (
    OutputFileError,
    create_pending_output,
    fund_output_quality,
    load_output,
    merge_improved_outputs,
    stable_fund_id,
    write_output,
    write_output_schema,
)


def test_stable_fund_id_is_deterministic() -> None:
    first = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com/",
    )

    second = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com",
    )

    assert stable_fund_id(first) == stable_fund_id(second)
    assert stable_fund_id(first).startswith("fund_")


def test_create_and_load_pending_output(
    tmp_path: Path,
) -> None:
    funds = [
        FundInput(
            name="Example SICAV a.s.",
            web="https://example.com",
        )
    ]

    output = create_pending_output(
        funds,
        now=datetime(
            2026,
            7,
            23,
            tzinfo=UTC,
        ),
    )

    output_path = tmp_path / "funds.enriched.json"

    write_output(
        output_path,
        output,
    )

    loaded = load_output(output_path)

    assert len(loaded) == 1
    assert loaded[0].name == "Example SICAV a.s."

    assert loaded[0].investment_horizon.status is FieldStatus.PENDING


def test_write_output_refuses_overwrite(
    tmp_path: Path,
) -> None:
    funds = [
        FundInput(
            name="Example SICAV a.s.",
            web="https://example.com",
        )
    ]

    output = create_pending_output(funds)

    output_path = tmp_path / "funds.enriched.json"

    write_output(
        output_path,
        output,
    )

    with pytest.raises(
        OutputFileError,
        match="already exists",
    ):
        write_output(
            output_path,
            output,
        )


def test_write_output_schema(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "funds-output.schema.json"

    write_output_schema(schema_path)

    payload = json.loads(schema_path.read_text(encoding="utf-8"))

    assert payload["type"] == "array"
    assert "$defs" in payload


def test_retry_merge_replaces_only_better_result() -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com",
    )

    base = create_pending_output([fund])[0]

    one_found = base.model_copy(
        update={
            "investment_horizon": (
                base.investment_horizon.model_copy(
                    update={
                        "status": FieldStatus.FOUND,
                    }
                )
            )
        }
    )

    two_found = one_found.model_copy(
        update={
            "minimum_investment": (
                one_found.minimum_investment.model_copy(
                    update={
                        "status": FieldStatus.FOUND,
                    }
                )
            )
        }
    )

    merged, summary = merge_improved_outputs(
        base_outputs=[one_found],
        retry_outputs=[two_found],
        retry_fund_ids={one_found.fund_id},
        minimum_found_improvement=1,
    )

    assert merged == [two_found]
    assert summary.replaced == 1
    assert summary.preserved == 0


def test_retry_merge_preserves_result_without_required_improvement() -> None:
    fund = FundInput(
        name="Example SICAV a.s.",
        web="https://example.com",
    )

    base = create_pending_output([fund])[0]

    one_found = base.model_copy(
        update={
            "investment_horizon": (
                base.investment_horizon.model_copy(
                    update={
                        "status": FieldStatus.FOUND,
                    }
                )
            )
        }
    )

    merged, summary = merge_improved_outputs(
        base_outputs=[one_found],
        retry_outputs=[one_found],
        retry_fund_ids={one_found.fund_id},
        minimum_found_improvement=1,
    )

    assert merged == [one_found]
    assert summary.replaced == 0
    assert summary.preserved == 1
    assert fund_output_quality(one_found)[0] == 1
