from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
)

from fundscraper.database import (
    list_parsed_documents,
)
from fundscraper.fallback_sources import (
    ApprovedFallbackSource,
    FallbackField,
)
from fundscraper.models import FundInput
from fundscraper.normalization import (
    canonical_domain,
)
from fundscraper.output_models import (
    FieldStatus,
    FundOutput,
    ProcessingStatus,
)
from fundscraper.output_service import (
    stable_fund_id,
)


class FallbackActionType(StrEnum):
    EXPANDED_CRAWL = "expanded_crawl"
    DOMAIN_ADAPTER = "domain_adapter"
    OCR = "ocr"
    APPROVED_SOURCE = "approved_source"
    GROUNDED_LLM = "grounded_llm"
    MANUAL_REVIEW = "manual_review"


class FallbackAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: FallbackField
    action: FallbackActionType

    priority: int = Field(ge=1)

    reason: str = Field(min_length=1)

    can_run_automatically: bool
    requires_adapter: bool

    source_name: str | None = None
    source_url: HttpUrl | None = None


class FundFallbackPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: str
    fund_name: str
    web: HttpUrl
    domain: str
    processing_status: ProcessingStatus

    missing_fields: list[FallbackField]

    actions: list[FallbackAction]


class FallbackPlanReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    funds_considered: int
    funds_requiring_fallback: int
    missing_field_counts: dict[str, int]

    plans: list[FundFallbackPlan]


class FallbackPlanError(RuntimeError):
    """Raised when a fallback plan cannot be written."""


def build_fallback_plan(
    *,
    funds: list[FundInput],
    outputs: list[FundOutput],
    database_path: Path,
    approved_sources: list[ApprovedFallbackSource],
    now: datetime | None = None,
) -> FallbackPlanReport:
    """Build ordered fallback actions for processed incomplete funds."""

    output_by_id = {output.fund_id: output for output in outputs}

    domain_counts = Counter(canonical_domain(fund.web) for fund in funds)

    plans: list[FundFallbackPlan] = []

    missing_field_counts: Counter[str] = Counter()

    for fund in funds:
        output = output_by_id.get(stable_fund_id(fund))

        if output is None:
            continue

        if output.processing.status in {
            ProcessingStatus.PENDING,
            ProcessingStatus.IN_PROGRESS,
        }:
            continue

        missing_fields = [
            field
            for field in FallbackField
            if _field_status(
                output,
                field,
            )
            is not FieldStatus.FOUND
        ]

        if not missing_fields:
            continue

        for field in missing_fields:
            missing_field_counts[field.value] += 1

        parsed_documents = list_parsed_documents(
            database_path,
            fund_id=output.fund_id,
        )

        has_parsed_documents = bool(parsed_documents)

        has_scanned_documents = any(document.scanned_candidate for document in parsed_documents)

        domain = canonical_domain(fund.web)

        actions: list[FallbackAction] = []

        for field in missing_fields:
            current_status = _field_status(
                output,
                field,
            )

            reason = _field_reason(
                output,
                field,
            )

            actions.append(
                FallbackAction(
                    field=field,
                    action=(FallbackActionType.EXPANDED_CRAWL),
                    priority=10,
                    reason=(
                        "Repeat the official-site crawl with greater "
                        "depth and additional document-page discovery. "
                        f"Current result: {current_status.value}. "
                        f"{reason}"
                    ),
                    can_run_automatically=True,
                    requires_adapter=False,
                )
            )

            if domain_counts[domain] >= 2:
                actions.append(
                    FallbackAction(
                        field=field,
                        action=(FallbackActionType.DOMAIN_ADAPTER),
                        priority=20,
                        reason=(
                            f"The domain {domain} is shared by "
                            f"{domain_counts[domain]} input funds. "
                            "A domain-specific adapter can identify "
                            "the exact fund page and documents."
                        ),
                        can_run_automatically=False,
                        requires_adapter=True,
                    )
                )

            if has_scanned_documents:
                actions.append(
                    FallbackAction(
                        field=field,
                        action=FallbackActionType.OCR,
                        priority=30,
                        reason=(
                            "At least one parsed PDF contains too "
                            "little extractable text and is marked "
                            "as a likely scanned document."
                        ),
                        can_run_automatically=False,
                        requires_adapter=False,
                    )
                )

            for source in approved_sources:
                if field not in source.supported_fields:
                    continue

                actions.append(
                    FallbackAction(
                        field=field,
                        action=(FallbackActionType.APPROVED_SOURCE),
                        priority=(100 + source.priority),
                        reason=(
                            f"Search the explicitly approved {source.source_type.value} source."
                        ),
                        can_run_automatically=False,
                        requires_adapter=True,
                        source_name=source.name,
                        source_url=source.base_url,
                    )
                )

            if has_parsed_documents:
                actions.append(
                    FallbackAction(
                        field=field,
                        action=(FallbackActionType.GROUNDED_LLM),
                        priority=500,
                        reason=(
                            "Parsed source text exists, but the "
                            "deterministic extractor did not produce "
                            "a reliable value. An LLM may inspect only "
                            "the supplied source text and must return "
                            "verbatim evidence."
                        ),
                        can_run_automatically=False,
                        requires_adapter=False,
                    )
                )

            actions.append(
                FallbackAction(
                    field=field,
                    action=(FallbackActionType.MANUAL_REVIEW),
                    priority=900,
                    reason=(
                        "Use manual review only after official-site, "
                        "OCR, approved-source and grounded extraction "
                        "fallbacks fail."
                    ),
                    can_run_automatically=False,
                    requires_adapter=False,
                )
            )

        plans.append(
            FundFallbackPlan(
                fund_id=output.fund_id,
                fund_name=fund.name,
                web=HttpUrl(fund.web),
                domain=domain,
                processing_status=(output.processing.status),
                missing_fields=missing_fields,
                actions=sorted(
                    actions,
                    key=lambda action: (
                        action.field.value,
                        action.priority,
                        action.action.value,
                    ),
                ),
            )
        )

    return FallbackPlanReport(
        generated_at=(now or datetime.now(UTC)),
        funds_considered=len(funds),
        funds_requiring_fallback=len(plans),
        missing_field_counts=dict(sorted(missing_field_counts.items())),
        plans=plans,
    )


def write_fallback_plan(
    *,
    report: FallbackPlanReport,
    path: Path,
) -> None:
    """Write a fallback plan as atomic JSON."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        temporary_path.write_text(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)

        raise FallbackPlanError(f"Fallback plan could not be written: {path}: {exc}") from exc


def _field_status(
    output: FundOutput,
    field: FallbackField,
) -> FieldStatus:
    match field:
        case FallbackField.INVESTMENT_HORIZON:
            return output.investment_horizon.status

        case FallbackField.MINIMUM_INVESTMENT:
            return output.minimum_investment.status

        case FallbackField.TARGET_RETURN:
            return output.target_return.status

        case FallbackField.FEES:
            return output.fees.status

        case FallbackField.ASSETS_UNDER_MANAGEMENT:
            return output.assets_under_management.status


def _field_reason(
    output: FundOutput,
    field: FallbackField,
) -> str:
    match field:
        case FallbackField.INVESTMENT_HORIZON:
            reason = output.investment_horizon.reason

        case FallbackField.MINIMUM_INVESTMENT:
            reason = output.minimum_investment.reason

        case FallbackField.TARGET_RETURN:
            reason = output.target_return.reason

        case FallbackField.FEES:
            reason = output.fees.reason

        case FallbackField.ASSETS_UNDER_MANAGEMENT:
            reason = output.assets_under_management.reason

    if reason is None:
        return "No structured missing-value reason was recorded."

    return f"{reason.code.value}: {reason.detail}"
