from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
    ProcessingStatus,
    TargetReturnValue,
)

OUTPUT_ADAPTER = TypeAdapter(list[FundOutput])


class OutputFileError(ValueError):
    """Raised when an enriched output file cannot be created or validated."""


def stable_fund_identifier(
    *,
    name: str,
    web: str | None,
) -> str:
    """
    Create a deterministic identifier from raw fund name and website.

    The website may be missing because the original input file can
    contain funds without a known official domain. Those records still
    need a stable identifier for reporting and later backfilling.
    """

    normalized_web = canonical_url(web) if web else ""

    normalized_key = f"{name.strip()}\n{normalized_web}"

    digest = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()[:16]

    return f"fund_{digest}"


def stable_fund_id(fund: FundInput) -> str:
    """Create a deterministic identifier from the original fund data."""

    return stable_fund_identifier(
        name=fund.name,
        web=fund.web,
    )


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


RETRY_FIELD_NAMES = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
)


@dataclass(frozen=True, slots=True)
class RetryMergeSummary:
    candidates: int
    replaced: int
    preserved: int
    missing_candidates: int


def fund_output_quality(
    output: FundOutput,
) -> tuple[int, int, int]:
    """Return a deterministic quality tuple for retry-result comparison."""

    status_weights = {
        FieldStatus.FOUND: 100,
        FieldStatus.CONFLICTING: 30,
        FieldStatus.AMBIGUOUS: 20,
        FieldStatus.NOT_FOUND: 10,
        FieldStatus.ERROR: 0,
        FieldStatus.PENDING: -1,
    }

    statuses = [
        getattr(
            output,
            field_name,
        ).status
        for field_name in RETRY_FIELD_NAMES
    ]

    found_count = sum(1 for status in statuses if status is FieldStatus.FOUND)

    weighted_status = sum(status_weights[status] for status in statuses)

    processing_weights = {
        ProcessingStatus.COMPLETED: 4,
        ProcessingStatus.PARTIAL: 3,
        ProcessingStatus.IN_PROGRESS: 2,
        ProcessingStatus.PENDING: 1,
        ProcessingStatus.FAILED: 0,
    }

    return (
        found_count,
        weighted_status,
        processing_weights[output.processing.status],
    )


def merge_improved_outputs(
    *,
    base_outputs: list[FundOutput],
    retry_outputs: list[FundOutput],
    retry_fund_ids: set[str],
    minimum_found_improvement: int = 1,
) -> tuple[list[FundOutput], RetryMergeSummary]:
    """Merge only strictly better retry results into the master output."""

    if minimum_found_improvement < 0:
        raise ValueError("minimum_found_improvement must be non-negative")

    retry_by_id = {output.fund_id: output for output in retry_outputs}

    merged_outputs: list[FundOutput] = []

    replaced = 0
    preserved = 0
    missing_candidates = 0

    for base_output in base_outputs:
        if base_output.fund_id not in retry_fund_ids:
            merged_outputs.append(base_output)
            continue

        candidate = retry_by_id.get(base_output.fund_id)

        if candidate is None:
            missing_candidates += 1
            preserved += 1
            merged_outputs.append(base_output)
            continue

        base_quality = fund_output_quality(base_output)

        candidate_quality = fund_output_quality(candidate)

        found_improvement = candidate_quality[0] - base_quality[0]

        should_replace = (
            found_improvement >= minimum_found_improvement and candidate_quality > base_quality
        )

        if should_replace:
            merged_outputs.append(candidate)
            replaced += 1
        else:
            merged_outputs.append(base_output)
            preserved += 1

    return (
        merged_outputs,
        RetryMergeSummary(
            candidates=len(retry_fund_ids),
            replaced=replaced,
            preserved=preserved,
            missing_candidates=missing_candidates,
        ),
    )
