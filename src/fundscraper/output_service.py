from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from fundscraper.models import FundInput
from fundscraper.normalization import canonical_url
from fundscraper.output_models import (
    AssetsUnderManagementValue,
    FeeCollection,
    FieldResult,
    FieldStatus,
    FundIdentity,
    FundOutput,
    InvestmentHorizonValue,
    MinimumInvestmentValue,
    ProcessingMetadata,
    TargetReturnValue,
)

OUTPUT_ADAPTER = TypeAdapter(list[FundOutput])


class OutputFileError(ValueError):
    """Raised when an enriched output file cannot be created or validated."""


def stable_fund_id(fund: FundInput) -> str:
    """Create a deterministic identifier from the original fund data."""

    normalized_key = f"{fund.name.strip()}\n{canonical_url(fund.web)}"

    digest = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()[:16]

    return f"fund_{digest}"


def create_pending_fund(
    fund: FundInput,
    *,
    now: datetime | None = None,
) -> FundOutput:
    """Create an empty validated output record for one fund."""

    current_time = now or datetime.now(UTC)

    return FundOutput(
        fund_id=stable_fund_id(fund),
        name=fund.name,
        web=fund.web,
        identity=FundIdentity(),
        investment_horizon=FieldResult[InvestmentHorizonValue](status=FieldStatus.PENDING),
        minimum_investment=FieldResult[MinimumInvestmentValue](status=FieldStatus.PENDING),
        target_return=FieldResult[TargetReturnValue](status=FieldStatus.PENDING),
        fees=FieldResult[FeeCollection](status=FieldStatus.PENDING),
        assets_under_management=FieldResult[AssetsUnderManagementValue](status=FieldStatus.PENDING),
        processing=ProcessingMetadata(updated_at=current_time),
    )


def create_pending_output(
    funds: list[FundInput],
    *,
    now: datetime | None = None,
) -> list[FundOutput]:
    """Create pending output records while preserving input order."""

    current_time = now or datetime.now(UTC)

    return [
        create_pending_fund(
            fund,
            now=current_time,
        )
        for fund in funds
    ]


def write_output(
    path: Path,
    funds: list[FundOutput],
    *,
    overwrite: bool = False,
) -> None:
    """Write validated output using an atomic temporary file."""

    if path.exists() and not overwrite:
        raise OutputFileError(f"Output file already exists: {path}")

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = OUTPUT_ADAPTER.dump_python(
        funds,
        mode="json",
    )

    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    temporary_path.write_text(
        serialized,
        encoding="utf-8",
    )

    temporary_path.replace(path)


def load_output(path: Path) -> list[FundOutput]:
    """Load and validate an enriched output JSON file."""

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise OutputFileError(f"Output file does not exist: {path}") from exc
    except OSError as exc:
        raise OutputFileError(f"Output file could not be read: {path}: {exc}") from exc

    try:
        payload: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise OutputFileError(
            f"Output file contains invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    try:
        return OUTPUT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise OutputFileError(f"Output validation failed:\n{exc}") from exc


def write_output_schema(path: Path) -> None:
    """Generate JSON Schema for the complete output array."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    schema = OUTPUT_ADAPTER.json_schema(mode="serialization")

    path.write_text(
        json.dumps(
            schema,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
