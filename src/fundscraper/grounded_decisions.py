from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from fundscraper.output_models import (
    Confidence,
    ReasonCode,
)


class GroundedDecisionError(RuntimeError):
    """Raised when grounded decisions are invalid."""


class GroundedDecisionStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"


class GroundedDecision(BaseModel):
    """One provider-independent grounded field decision."""

    model_config = ConfigDict(extra="forbid")

    packet_id: str = Field(min_length=1)

    status: GroundedDecisionStatus

    value: dict[str, Any] | None = None

    snippet_ids: list[str] = Field(default_factory=list)

    reason_code: ReasonCode | None = None

    reason_detail: str | None = None

    confidence: Confidence = Confidence.MEDIUM

    @model_validator(mode="after")
    def validate_decision(
        self,
    ) -> GroundedDecision:
        if self.status is GroundedDecisionStatus.FOUND:
            if self.value is None:
                raise ValueError("A found grounded decision requires a value")

            if not self.snippet_ids:
                raise ValueError("A found grounded decision requires at least one snippet_id")

            if self.reason_code is not None:
                raise ValueError("A found grounded decision must not contain reason_code")

            return self

        if self.value is not None:
            raise ValueError("A non-found grounded decision must not contain a value")

        if not self.reason_detail:
            raise ValueError("A non-found grounded decision requires reason_detail")

        return self


class GroundedDecisionFile(BaseModel):
    """Validated response contract for any grounded provider."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=1,
        ge=1,
        le=1,
    )

    provider: str = Field(min_length=1)

    generated_at: datetime

    decisions: list[GroundedDecision]

    @model_validator(mode="after")
    def validate_unique_packets(
        self,
    ) -> GroundedDecisionFile:
        packet_ids: set[str] = set()

        for decision in self.decisions:
            if decision.packet_id in packet_ids:
                raise ValueError(
                    f"Grounded decisions contain duplicate packet_id: {decision.packet_id}"
                )

            packet_ids.add(decision.packet_id)

        return self


def load_grounded_decisions(
    path: Path,
) -> GroundedDecisionFile:
    """Load and validate grounded provider decisions."""

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise GroundedDecisionError(f"Grounded decision file does not exist: {path}") from exc
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise GroundedDecisionError(
            f"Grounded decision file could not be loaded: {path}: {exc}"
        ) from exc

    try:
        return GroundedDecisionFile.model_validate(payload)
    except ValidationError as exc:
        raise GroundedDecisionError(f"Grounded decision file is invalid:\n{exc}") from exc
