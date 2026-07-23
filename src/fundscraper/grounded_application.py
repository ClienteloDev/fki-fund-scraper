from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import HttpUrl, ValidationError

from fundscraper.fallback_sources import (
    FallbackField,
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
from fundscraper.output_models import (
    AssetsUnderManagementValue,
    Confidence,
    DataScope,
    Evidence,
    ExtractionMetadata,
    ExtractionMethod,
    FeeCollection,
    FieldResult,
    FieldStatus,
    FundOutput,
    InvestmentHorizonValue,
    MinimumInvestmentValue,
    MissingReason,
    ProcessingMetadata,
    ProcessingStatus,
    ReasonCode,
    ScopeType,
    SourceAttempt,
    SourceMetadata,
    TargetReturnValue,
)
from fundscraper.output_service import (
    load_output,
    write_output,
)


@dataclass(frozen=True, slots=True)
class GroundedApplicationSummary:
    provider: str
    decisions_received: int
    decisions_applied: int
    found_applied: int
    unresolved_applied: int
    funds_updated: int
    output_path: Path


def apply_grounded_decisions(
    *,
    packet_report: GroundingBatchReport,
    decision_file: GroundedDecisionFile,
    output_path: Path,
    allow_overwrite: bool = False,
    now: datetime | None = None,
) -> GroundedApplicationSummary:
    """Validate and apply grounded decisions to enriched output."""

    outputs = load_output(output_path)

    packet_by_id = {packet.packet_id: packet for packet in packet_report.packets}

    output_index_by_id = {output.fund_id: index for index, output in enumerate(outputs)}

    updated_fund_ids: set[str] = set()

    found_applied = 0
    unresolved_applied = 0

    update_time = now or datetime.now(UTC)

    for decision in decision_file.decisions:
        packet = packet_by_id.get(decision.packet_id)

        if packet is None:
            raise GroundedDecisionError(
                f"Grounded decision references an unknown packet: {decision.packet_id}"
            )

        output_index = output_index_by_id.get(packet.fund_id)

        if output_index is None:
            raise GroundedDecisionError(
                f"Grounding packet fund does not exist in output: {packet.fund_id}"
            )

        _validate_snippet_references(
            packet=packet,
            decision=decision,
        )

        current_output = outputs[output_index]

        current_result = _get_field_result(
            output=current_output,
            field=packet.field,
        )

        if current_result.status is FieldStatus.FOUND and not allow_overwrite:
            raise GroundedDecisionError(
                f"Grounded decision would overwrite an already found value: {decision.packet_id}"
            )

        grounded_result = _build_grounded_result(
            packet=packet,
            decision=decision,
        )

        updated_output = _set_field_result(
            output=current_output,
            field=packet.field,
            result=grounded_result,
        )

        outputs[output_index] = updated_output

        updated_fund_ids.add(packet.fund_id)

        if decision.status is GroundedDecisionStatus.FOUND:
            found_applied += 1
        else:
            unresolved_applied += 1

    for fund_id in updated_fund_ids:
        output_index = output_index_by_id[fund_id]

        output = outputs[output_index]

        processing_status = (
            ProcessingStatus.COMPLETED if _all_fields_found(output) else ProcessingStatus.PARTIAL
        )

        outputs[output_index] = output.model_copy(
            update={
                "processing": ProcessingMetadata(
                    status=processing_status,
                    updated_at=update_time,
                    warnings=list(output.processing.warnings),
                )
            }
        )

    write_output(
        output_path,
        outputs,
        overwrite=True,
    )

    return GroundedApplicationSummary(
        provider=decision_file.provider,
        decisions_received=len(decision_file.decisions),
        decisions_applied=len(decision_file.decisions),
        found_applied=found_applied,
        unresolved_applied=unresolved_applied,
        funds_updated=len(updated_fund_ids),
        output_path=output_path,
    )


def _validate_snippet_references(
    *,
    packet: FieldGroundingPacket,
    decision: GroundedDecision,
) -> None:
    available_snippet_ids = {snippet.snippet_id for snippet in packet.snippets}

    unknown_snippet_ids = [
        snippet_id for snippet_id in decision.snippet_ids if snippet_id not in available_snippet_ids
    ]

    if unknown_snippet_ids:
        raise GroundedDecisionError(
            "Grounded decision references snippets outside its "
            f"packet {packet.packet_id}: "
            f"{', '.join(unknown_snippet_ids)}"
        )


def _build_grounded_result(
    *,
    packet: FieldGroundingPacket,
    decision: GroundedDecision,
) -> FieldResult[Any]:
    if decision.status is GroundedDecisionStatus.FOUND:
        return _build_found_result(
            packet=packet,
            decision=decision,
        )

    return _build_unresolved_result(
        packet=packet,
        decision=decision,
    )


def _build_found_result(
    *,
    packet: FieldGroundingPacket,
    decision: GroundedDecision,
) -> FieldResult[Any]:
    if decision.value is None:
        raise GroundedDecisionError(f"Found decision has no value: {decision.packet_id}")

    value = _validate_field_value(
        field=packet.field,
        payload=decision.value,
    )

    snippet_by_id = {snippet.snippet_id: snippet for snippet in packet.snippets}

    primary_snippet = snippet_by_id[decision.snippet_ids[0]]

    confidence = decision.confidence

    review_required = confidence is Confidence.LOW or primary_snippet.scope_warning

    return FieldResult[Any](
        status=FieldStatus.FOUND,
        value=value,
        raw_value=primary_snippet.quote,
        scope=DataScope(
            type=ScopeType.FUND,
            fund_name=packet.fund_name,
        ),
        source=Evidence(
            source=SourceMetadata(
                url=HttpUrl(str(primary_snippet.url)),
                document_type=(primary_snippet.document_type),
                retrieved_at=(primary_snippet.retrieved_at),
                title=primary_snippet.title,
            ),
            quote=primary_snippet.quote,
            page=primary_snippet.page,
        ),
        extraction=ExtractionMetadata(
            method=ExtractionMethod.GROUNDED,
            confidence=confidence,
            review_required=review_required,
        ),
    )


def _build_unresolved_result(
    *,
    packet: FieldGroundingPacket,
    decision: GroundedDecision,
) -> FieldResult[Any]:
    status = _decision_field_status(decision.status)

    reason_code = decision.reason_code or _default_reason_code(decision.status)

    snippet_by_id = {snippet.snippet_id: snippet for snippet in packet.snippets}

    selected_snippets = [snippet_by_id[snippet_id] for snippet_id in decision.snippet_ids]

    if not selected_snippets:
        selected_snippets = list(packet.snippets)

    attempts = [
        _snippet_source_attempt(
            snippet=snippet,
            outcome=reason_code,
            detail=(
                decision.reason_detail or "Grounded review did not establish a reliable value."
            ),
        )
        for snippet in selected_snippets
    ]

    return FieldResult[Any](
        status=status,
        reason=MissingReason(
            code=reason_code,
            detail=(
                decision.reason_detail or "Grounded review did not establish a reliable value."
            ),
        ),
        attempted_sources=attempts,
    )


def _validate_field_value(
    *,
    field: FallbackField,
    payload: dict[str, Any],
) -> Any:
    try:
        match field:
            case FallbackField.INVESTMENT_HORIZON:
                return InvestmentHorizonValue.model_validate(payload)

            case FallbackField.MINIMUM_INVESTMENT:
                return MinimumInvestmentValue.model_validate(payload)

            case FallbackField.TARGET_RETURN:
                return TargetReturnValue.model_validate(payload)

            case FallbackField.FEES:
                return FeeCollection.model_validate(payload)

            case FallbackField.ASSETS_UNDER_MANAGEMENT:
                return AssetsUnderManagementValue.model_validate(payload)

    except ValidationError as exc:
        raise GroundedDecisionError(
            f"Grounded value is invalid for field {field.value}:\n{exc}"
        ) from exc

    raise GroundedDecisionError(f"Unsupported grounded field: {field}")


def _get_field_result(
    *,
    output: FundOutput,
    field: FallbackField,
) -> FieldResult[Any]:
    match field:
        case FallbackField.INVESTMENT_HORIZON:
            return output.investment_horizon

        case FallbackField.MINIMUM_INVESTMENT:
            return output.minimum_investment

        case FallbackField.TARGET_RETURN:
            return output.target_return

        case FallbackField.FEES:
            return output.fees

        case FallbackField.ASSETS_UNDER_MANAGEMENT:
            return output.assets_under_management

    raise GroundedDecisionError(f"Unsupported grounded field: {field}")


def _set_field_result(
    *,
    output: FundOutput,
    field: FallbackField,
    result: FieldResult[Any],
) -> FundOutput:
    return output.model_copy(update={field.value: result})


def _decision_field_status(
    status: GroundedDecisionStatus,
) -> FieldStatus:
    match status:
        case GroundedDecisionStatus.NOT_FOUND:
            return FieldStatus.NOT_FOUND

        case GroundedDecisionStatus.AMBIGUOUS:
            return FieldStatus.AMBIGUOUS

        case GroundedDecisionStatus.CONFLICTING:
            return FieldStatus.CONFLICTING

        case GroundedDecisionStatus.FOUND:
            return FieldStatus.FOUND

    raise GroundedDecisionError(f"Unsupported grounded status: {status}")


def _default_reason_code(
    status: GroundedDecisionStatus,
) -> ReasonCode:
    match status:
        case GroundedDecisionStatus.NOT_FOUND:
            return ReasonCode.NOT_QUANTIFIED

        case GroundedDecisionStatus.AMBIGUOUS:
            return ReasonCode.SCOPE_MISMATCH

        case GroundedDecisionStatus.CONFLICTING:
            return ReasonCode.CONFLICTING_VALUES

        case GroundedDecisionStatus.FOUND:
            raise GroundedDecisionError("Found grounded decision does not require a reason")

    raise GroundedDecisionError(f"Unsupported grounded status: {status}")


def _snippet_source_attempt(
    *,
    snippet: GroundingSnippet,
    outcome: ReasonCode,
    detail: str,
) -> SourceAttempt:
    return SourceAttempt(
        url=HttpUrl(str(snippet.url)),
        retrieved_at=snippet.retrieved_at,
        outcome=outcome,
        document_type=snippet.document_type,
        detail=detail,
    )


def _all_fields_found(
    output: FundOutput,
) -> bool:
    return all(
        result.status is FieldStatus.FOUND
        for result in (
            output.investment_horizon,
            output.minimum_investment,
            output.target_return,
            output.fees,
            output.assets_under_management,
        )
    )
