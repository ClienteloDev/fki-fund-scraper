from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import HttpUrl, ValidationError

from fundscraper.output_models import (
    Confidence,
    DocumentType,
    Evidence,
    ExtractionMetadata,
    ExtractionMethod,
    FieldResult,
    FieldStatus,
    InvestmentHorizonValue,
    MissingReason,
    ReasonCode,
    SourceMetadata,
    TargetReturnValue,
)


def test_found_result_requires_evidence() -> None:
    with pytest.raises(
        ValidationError,
        match="source evidence",
    ):
        FieldResult[InvestmentHorizonValue](
            status=FieldStatus.FOUND,
            value=InvestmentHorizonValue(recommended_years=5),
            raw_value="5 years",
            extraction=ExtractionMetadata(
                method=ExtractionMethod.REGEX,
                confidence=Confidence.HIGH,
            ),
        )


def test_not_found_result_requires_reason() -> None:
    with pytest.raises(
        ValidationError,
        match="must contain reason",
    ):
        FieldResult[InvestmentHorizonValue](status=FieldStatus.NOT_FOUND)


def test_valid_found_result() -> None:
    result = FieldResult[InvestmentHorizonValue](
        status=FieldStatus.FOUND,
        value=InvestmentHorizonValue(recommended_years=5),
        raw_value="Doporučená doba držení je 5 let.",
        source=Evidence(
            source=SourceMetadata(
                url=HttpUrl("https://example.com/kid.pdf"),
                document_type=DocumentType.PRIIPS_KID,
                retrieved_at=datetime(
                    2026,
                    7,
                    23,
                    tzinfo=UTC,
                ),
                published_at=date(
                    2026,
                    1,
                    1,
                ),
            ),
            quote="Doporučená doba držení je 5 let.",
            page=3,
        ),
        extraction=ExtractionMetadata(
            method=ExtractionMethod.REGEX,
            confidence=Confidence.HIGH,
        ),
    )

    assert result.value is not None
    assert result.value.recommended_years == 5


def test_target_return_rejects_empty_value() -> None:
    with pytest.raises(
        ValidationError,
        match="exact value or a range",
    ):
        TargetReturnValue()


def test_target_return_rejects_reversed_range() -> None:
    with pytest.raises(
        ValidationError,
        match="must not exceed",
    ):
        TargetReturnValue(
            minimum_percent_pa=10,
            maximum_percent_pa=5,
        )


def test_valid_not_found_result() -> None:
    result = FieldResult[InvestmentHorizonValue](
        status=FieldStatus.NOT_FOUND,
        reason=MissingReason(
            code=ReasonCode.NOT_PUBLICLY_DISCLOSED,
            detail=("No public source states the investment horizon."),
        ),
    )

    assert result.value is None
