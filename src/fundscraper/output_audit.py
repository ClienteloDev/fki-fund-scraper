from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field

from fundscraper.domain_candidates import (
    EXCLUDED_DOMAINS,
    distinctive_name_tokens,
)
from fundscraper.extended_validation import (
    AUDIT_RULESET_VERSION,
    AUDIT_SCHEMA_VERSION,
    FIELD_SPECIFICATIONS,
    FundRecordView,
    ValidationCode,
    ValidationFinding,
    ValidationSeverity,
    ValueProvenance,
    validate_annual_returns,
    validate_assets_under_management,
    validate_capital_attribution,
    validate_capital_metric_wording,
    validate_capital_observations,
    validate_cross_fields,
    validate_fee_items,
    validate_historical_series,
    validate_horizon_wording,
    validate_investment_horizon,
    validate_minimum_investment,
    validate_news_items,
    validate_party,
    validate_provenance,
    validate_return_wording,
    validate_target_return,
)
from fundscraper.field_extraction import FEE_KEYWORDS
from fundscraper.html_discovery import normalize_search_text
from fundscraper.normalization import canonical_domain
from fundscraper.output_models import (
    AnnualReturnObservation,
    AssetsUnderManagementValue,
    CapitalObservation,
    FeeItem,
    FundNewsItem,
    FundParty,
    HistoricalValueSeries,
    InvestmentHorizonValue,
    MinimumInvestmentValue,
    PartyRole,
    TargetReturnValue,
)
from fundscraper.output_service import stable_fund_identifier

AUDITED_FIELDS: Final[tuple[str, ...]] = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
)


# The fields added in schema version 3. They are audited only when the
# file actually carries them, so a delivered file written before they
# existed is not reported as missing six fields per fund.
EXTENDED_AUDITED_FIELDS: Final[tuple[str, ...]] = (
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
)


ALL_AUDITED_FIELDS: Final[tuple[str, ...]] = AUDITED_FIELDS + EXTENDED_AUDITED_FIELDS


class OutputAuditError(RuntimeError):
    """Raised when the enriched output file cannot be audited."""


class AuditStatus(StrEnum):
    """
    What the audit concluded about one field of one fund.

    ``SUSPICIOUS`` and worse never delete anything: the value, its source,
    its evidence and its confidence stay in the delivered file, and the
    status only says how far a reader may trust them. ``REJECTED`` is
    reserved for a value whose meaning is wrong, not merely unusual.
    """

    VALID = "valid"
    SUSPICIOUS = "suspicious"
    CONFLICTING = "conflicting"
    REJECTED = "rejected"
    MISSING = "missing"


# How a rule severity is reported. The three levels of the validation
# vocabulary map one to one, so a reader of the audit and a reader of the
# extraction see the same word for the same defect.
_SEVERITY_STATUS: Final[dict[ValidationSeverity, AuditStatus]] = {
    ValidationSeverity.REVIEW: AuditStatus.SUSPICIOUS,
    ValidationSeverity.CONFLICT: AuditStatus.CONFLICTING,
    ValidationSeverity.REJECT: AuditStatus.REJECTED,
}


_STATUS_RANK: Final[dict[AuditStatus, int]] = {
    AuditStatus.VALID: 0,
    AuditStatus.MISSING: 0,
    AuditStatus.SUSPICIOUS: 1,
    AuditStatus.CONFLICTING: 2,
    AuditStatus.REJECTED: 3,
}


class SourceType(StrEnum):
    """Where the value came from, relative to the fund itself."""

    OFFICIAL_WEBSITE = "official_website"
    MANAGER_OR_ADMINISTRATOR = "manager_or_administrator"
    THIRD_PARTY = "third_party"
    NONE = "none"


# Hosts of Czech fund managers and administrators. A value taken from one
# of them is legitimate, but it carries a higher risk of being attributed
# to the wrong fund, because one page lists many funds.
MANAGER_HOST_FRAGMENTS: Final[tuple[str, ...]] = (
    "avantfunds",
    "amista",
    "codyainvest",
    "monecois",
    "jtis",
    "deltais",
    "encoram",
    "tillerfunds",
    "creditas",
    "redsidefunds",
    "bhs",
    "natland",
    "conseq",
    "generali",
    "raiffeisen",
    "wood",
)


# A fee label and its value stand next to each other in a clean source
# line. A large distance means the PDF columns were interleaved and the
# number belongs to a different sentence.
FEE_LABEL_VALUE_MAXIMUM_DISTANCE: Final[int] = 60


FEE_LABEL_KEYWORDS: Final[tuple[str, ...]] = (
    "poplatek",
    "odmena",
    "naklady",
    "srazka",
    "prirazka",
    "fee",
    "charges",
)


# Wording showing a percentage describes how income is split rather than
# what the investor pays.
FEE_INCOME_SHARE_MARKERS: Final[tuple[str, ...]] = (
    "je prijmem",
    # The number stands inside the phrase as often as it follows it:
    # "Vystupni poplatek (srazka) je 100 % prijmem do fondu" splits
    # "je prijmem" in two, which is why one fund delivered an exit fee
    # of the entire investment.
    "prijmem",
    "prijmem spolecnosti",
    "nalezi",
    "pripise",
    "ve prospech",
)


FEE_MAXIMUM_MARKERS: Final[tuple[str, ...]] = (
    "max.",
    "max ",
    "maximaln",
    "nejvyse",
    " az ",
    "up to",
)


# Wording that proves a fee is a range, so its lower bound is not the fee.
FEE_RANGE_MARKERS: Final[tuple[str, ...]] = (
    "od ",
    " do ",
    " az ",
    "maximaln",
    "nejvyse",
    "up to",
    "minimaln",
)


# Wording that explicitly supports a zero fee. A key information
# document states one in its cost table as a plain amount — "U tohoto
# produktu se neplati zadny vykonnostni poplatek. 0 CZK" — so the
# amount forms belong here next to the rate forms.
FEE_ZERO_MARKERS: Final[tuple[str, ...]] = (
    "0 %",
    "0%",
    "0,00 %",
    "0.00 %",
    "0 czk",
    "0 eur",
    "0 usd",
    "0 kc",
    "bez poplatku",
    "neplati zadny",
    "neni aplikovan zadny",
    "neuctuje zadny",
    "nehradi zadny",
    "zdarma",
    "no fee",
    "is charged",
    "free of charge",
)


# A unit written next to the amount itself, proving the conversion was
# faithful even when the resulting value is small.
AUM_UNIT_MARKERS: Final[tuple[str, ...]] = (
    "tis. kc",
    "tis.kc",
    "tisic",
    "mil. kc",
    "mil.kc",
    "milion",
    "mld",
)


ANNUAL_REPORT_URL_MARKERS: Final[tuple[str, ...]] = (
    "vyrocni",
    "vyrocka",
    "/vz_",
    "vz_",
    "-vz-",
    "annual",
    "zaverka",
)


# The upper bound of a fee range. Interleaved PDF columns often separate
# the lower bound from it, leaving a fragment such as "% do 3 % z vyse
# investice", so the bound is recognised with or without its counterpart.
FEE_RANGE_BOUNDS_PATTERN: Final = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*%\s*)?(?:-|az|do|to)\s*(?P<maximum>\d+(?:[.,]\d+)?)\s*%",
)


class AuditFinding(BaseModel):
    """One field of one fund that needs attention."""

    model_config = ConfigDict(extra="forbid")

    fund_id: str
    fund_name: str
    field: str
    status: AuditStatus
    severity: ValidationSeverity
    reason_code: str
    reason: str
    recommended_action: str

    extracted_value: Any = None
    normalized_value: str | None = None

    source_url: str | None = None
    source_type: SourceType = SourceType.NONE
    source_date: str | None = None
    source_scope: str | None = None
    evidence: str | None = None
    related_funds: list[str] = Field(default_factory=list)


class AuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    funds: int
    fields_checked: int
    by_status: dict[str, int]
    by_field: dict[str, dict[str, int]]
    by_reason: dict[str, int]
    by_source_type: dict[str, int]

    # Added in step 4. Older reports simply do not carry them.
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_scope: dict[str, int] = Field(default_factory=dict)
    by_reason_status: dict[str, dict[str, int]] = Field(default_factory=dict)
    weakest_fields: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    input_path: str
    input_sha256: str

    # What produced this report: the shape of the file, and the rules
    # that decided its verdicts. Delivery refuses a report whose ruleset
    # is not the current one, because the same input audited by older
    # rules yields a different, weaker set of findings.
    schema_version: str = AUDIT_SCHEMA_VERSION
    ruleset_version: str = AUDIT_RULESET_VERSION

    schema_notes: list[str] = Field(default_factory=list)
    summary: AuditSummary
    findings: list[AuditFinding] = Field(default_factory=list)

    # Added in step 4: the rule each field was measured against, so a
    # reader of a finding can see what was expected of the value.
    field_specifications: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # A handful of findings per status, chosen to be read rather than
    # counted. The full list stays in ``findings``.
    review_examples: list[AuditFinding] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FieldOutcome:
    """The audit result of one field of one fund."""

    status: AuditStatus
    findings: tuple[AuditFinding, ...]


def load_enriched_records(
    path: Path,
) -> list[dict[str, Any]]:
    """
    Read the enriched output without enforcing the full output model.

    The delivered file carries a reduced schema, so validating it against
    FundOutput would fail before anything could be audited.
    """

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise OutputAuditError(f"Enriched output does not exist: {path}") from exc
    except OSError as exc:
        raise OutputAuditError(f"Enriched output could not be read: {path}: {exc}") from exc

    try:
        payload: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise OutputAuditError(
            f"Enriched output contains invalid JSON at line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, list):
        raise OutputAuditError("The root JSON value must be an array of funds")

    records: list[dict[str, Any]] = []

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise OutputAuditError(f"Fund record {index} must be a JSON object")

        records.append(item)

    return records


def file_digest(
    path: Path,
) -> str:
    """Return the SHA-256 of the audited file, proving what was read."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_enriched_output(
    *,
    records: list[dict[str, Any]],
    input_path: Path,
    input_sha256: str,
    now: datetime | None = None,
) -> AuditReport:
    """Classify every audited field of every fund in the output."""

    shared_sources = _shared_source_urls(records)

    findings: list[AuditFinding] = []

    by_status: Counter[str] = Counter()

    by_field: dict[str, Counter[str]] = {field: Counter() for field in ALL_AUDITED_FIELDS}

    by_reason: Counter[str] = Counter()

    by_source_type: Counter[str] = Counter()

    by_severity: Counter[str] = Counter()

    by_scope: Counter[str] = Counter()

    by_reason_status: dict[str, Counter[str]] = defaultdict(Counter)

    fields_checked = 0

    today = (now or datetime.now(UTC)).date()

    for record in records:
        fund_name = str(record.get("name") or "")

        fund_web = record.get("web")

        fund_id = stable_fund_identifier(
            name=fund_name,
            web=fund_web if isinstance(fund_web, str) else None,
        )

        # The cross-field rules read the whole record at once. Each of
        # their findings names the field a reviewer has to open, and is
        # reported as part of that field rather than beside it, so a fund
        # whose assets contradict its own capital series is not counted
        # as having a clean assets field.
        cross_findings: dict[str, list[AuditFinding]] = defaultdict(list)

        for finding in audit_cross_fields(
            fund_id=fund_id,
            fund_name=fund_name,
            record=record,
        ):
            cross_findings[finding.field].append(finding)

        for field in ALL_AUDITED_FIELDS:
            if field in EXTENDED_AUDITED_FIELDS and field not in record:
                # The file predates schema version 3. Counting a field it
                # never carried as missing would misreport the delivery.
                continue

            fields_checked += 1

            outcome = audit_field(
                fund_id=fund_id,
                fund_name=fund_name,
                fund_web=fund_web if isinstance(fund_web, str) else None,
                field=field,
                payload=record.get(field),
                shared_sources=shared_sources,
                today=today,
                extra_findings=tuple(cross_findings.get(field, ())),
            )

            by_status[outcome.status.value] += 1

            by_field[field][outcome.status.value] += 1

            findings.extend(outcome.findings)

    for finding in findings:
        by_reason[finding.reason_code] += 1

        by_source_type[finding.source_type.value] += 1

        by_severity[finding.severity.value] += 1

        by_scope[finding.source_scope or "unknown"] += 1

        by_reason_status[finding.reason_code][finding.status.value] += 1

    return AuditReport(
        generated_at=(now or datetime.now(UTC)),
        input_path=str(input_path),
        input_sha256=input_sha256,
        schema_version=AUDIT_SCHEMA_VERSION,
        ruleset_version=AUDIT_RULESET_VERSION,
        schema_notes=_schema_notes(records),
        summary=AuditSummary(
            funds=len(records),
            fields_checked=fields_checked,
            by_status=dict(sorted(by_status.items())),
            by_field={field: dict(sorted(counts.items())) for field, counts in by_field.items()},
            by_reason=dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
            by_source_type=dict(sorted(by_source_type.items())),
            by_severity=dict(sorted(by_severity.items())),
            by_scope=dict(sorted(by_scope.items())),
            by_reason_status={
                reason: dict(sorted(counts.items()))
                for reason, counts in sorted(by_reason_status.items())
            },
            weakest_fields=_weakest_fields(by_field),
        ),
        findings=findings,
        field_specifications=_published_specifications(),
        review_examples=_review_examples(findings),
    )


def _weakest_fields(
    by_field: dict[str, Counter[str]],
) -> list[str]:
    """
    Order the fields by how little of what they deliver can be trusted.

    A field that delivers ten values of which five are doubtful is weaker
    than one that delivers two hundred with twenty doubtful, so the share
    of doubted values decides, not their count.
    """

    ranked: list[tuple[float, int, str]] = []

    for field, counts in by_field.items():
        delivered = sum(
            counts.get(status.value, 0)
            for status in (
                AuditStatus.VALID,
                AuditStatus.SUSPICIOUS,
                AuditStatus.CONFLICTING,
                AuditStatus.REJECTED,
            )
        )

        if not delivered:
            continue

        doubted = delivered - counts.get(AuditStatus.VALID.value, 0)

        ranked.append(
            (
                doubted / delivered,
                doubted,
                field,
            )
        )

    ranked.sort(
        key=lambda item: (-item[0], -item[1], item[2]),
    )

    return [f"{field} ({share:.0%} of {doubted + 0} doubted)" for share, doubted, field in ranked]


def _published_specifications() -> dict[str, dict[str, Any]]:
    """Return the field specifications in the shape the report carries."""

    return {
        field: {
            "value_kind": specification.value_kind,
            "unit": specification.unit,
            "currencies": list(specification.currencies),
            "minimum": specification.minimum,
            "maximum": specification.maximum,
            "requires_as_of_date": specification.requires_as_of_date,
            "requires_source": specification.requires_source,
            "requires_evidence": specification.requires_evidence,
            "requires_page_in_paginated_sources": (
                specification.requires_page_in_paginated_sources
            ),
            "accepted_scopes": [scope.value for scope in specification.accepted_scopes],
            "hard_reject": [code.value for code in specification.hard_reject],
            "review_only": [code.value for code in specification.review_only],
            "notes": specification.notes,
        }
        for field, specification in FIELD_SPECIFICATIONS.items()
    }


# How many findings of each status are quoted in the report as examples.
REVIEW_EXAMPLES_PER_STATUS: Final = 5


def _review_examples(
    findings: Sequence[AuditFinding],
) -> list[AuditFinding]:
    """Pick a readable handful of findings, spread across reason codes."""

    chosen: list[AuditFinding] = []

    for status in (
        AuditStatus.REJECTED,
        AuditStatus.CONFLICTING,
        AuditStatus.SUSPICIOUS,
    ):
        seen: set[str] = set()

        for finding in findings:
            if finding.status is not status or finding.reason_code in seen:
                continue

            seen.add(finding.reason_code)

            chosen.append(finding)

            if len(seen) >= REVIEW_EXAMPLES_PER_STATUS:
                break

    return chosen


def audit_field(
    *,
    fund_id: str,
    fund_name: str,
    fund_web: str | None,
    field: str,
    payload: Any,
    shared_sources: dict[str, set[str]],
    today: date | None = None,
    extra_findings: tuple[AuditFinding, ...] = (),
) -> FieldOutcome:
    """Classify one field of one fund."""

    today = today or datetime.now(UTC).date()

    if not isinstance(payload, dict):
        return FieldOutcome(
            status=AuditStatus.MISSING,
            findings=(),
        )

    status = str(payload.get("status") or "")

    value = payload.get("value")

    source, quote = _source_and_quote(payload.get("source"))

    source_url = str(source.get("url")) if source and source.get("url") else None

    source_date = str(source.get("retrieved_at")) if source and source.get("retrieved_at") else None

    source_type = classify_source(
        source_url=source_url,
        fund_web=fund_web,
    )

    if status != "found" or value is None:
        return FieldOutcome(
            status=AuditStatus.MISSING,
            findings=(),
        )

    scope = payload.get("scope")

    extraction = payload.get("extraction")

    context = _FieldContext(
        fund_id=fund_id,
        fund_name=fund_name,
        fund_web=fund_web,
        field=field,
        source_url=source_url,
        source_type=source_type,
        source_date=source_date,
        quote=quote,
        scope_type=(
            str(scope.get("type")) if isinstance(scope, dict) and scope.get("type") else None
        ),
        page=_page_of(payload.get("source")),
        confidence=(
            str(extraction.get("confidence"))
            if isinstance(extraction, dict) and extraction.get("confidence")
            else None
        ),
        review_required=(
            bool(extraction.get("review_required")) if isinstance(extraction, dict) else None
        ),
        today=today,
        carries_evidence=_carries_evidence(payload.get("source")),
    )

    findings: list[AuditFinding] = list(extra_findings)

    findings.extend(
        _audit_source_evidence(
            context=context,
            shared_sources=shared_sources,
        )
    )

    checker = _FIELD_CHECKERS.get(field)

    if checker is not None:
        findings.extend(
            checker(
                context,
                value,
            )
        )

    findings.extend(
        _audit_traceability(
            context=context,
            value=value,
        )
    )

    if not findings:
        return FieldOutcome(
            status=AuditStatus.VALID,
            findings=(),
        )

    worst = max(
        (finding.status for finding in findings),
        key=_STATUS_RANK.__getitem__,
    )

    return FieldOutcome(
        status=worst,
        findings=tuple(findings),
    )


def _carries_evidence(
    payload: Any,
) -> bool:
    """
    Return whether the file stores the quoted source text of this field.

    The pipeline output wraps the source in an evidence object carrying
    the quote and the page. The reduced delivery schema has no place for
    either, and reporting each of its fields as unevidenced would describe
    the schema instead of the data. The schema notes say so once.
    """

    return isinstance(payload, dict) and isinstance(payload.get("source"), dict)


def _page_of(
    payload: Any,
) -> int | None:
    """Return the page a delivered evidence object names, if it names one."""

    if not isinstance(payload, dict):
        return None

    page = payload.get("page")

    return page if isinstance(page, int) and not isinstance(page, bool) else None


def _audit_traceability(
    *,
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """
    Check that a found value can be traced back to what it was read from.

    The value is kept whatever this finds. What a missing quote, a missing
    page or a number that does not appear in the evidence take away is the
    right to be believed without opening the source.
    """

    return _from_validation(
        context=context,
        findings=validate_provenance(
            ValueProvenance(
                field=context.field,
                source_url=context.source_url,
                retrieved_at=context.source_date,
                quote=context.quote,
                page=context.page,
                scope_type=context.scope_type,
                confidence=context.confidence,
                review_required=context.review_required,
                supporting_numbers=_supporting_numbers(
                    field=context.field,
                    value=value,
                ),
                evidence_available=context.carries_evidence,
            )
        ),
        value=value,
    )


def _supporting_numbers(
    *,
    field: str,
    value: Any,
) -> tuple[float, ...]:
    """Return the numbers a reader has to find in the quoted source text."""

    if not isinstance(value, dict):
        return ()

    if field == "investment_horizon":
        return _numbers(value.get("recommended_years"))

    if field in {
        "minimum_investment",
        "assets_under_management",
    }:
        return _numbers(value.get("amount"))

    if field == "target_return":
        return _numbers(
            value.get("value_percent_pa"),
            value.get("minimum_percent_pa"),
            value.get("maximum_percent_pa"),
        )

    # A collection or a series quotes a table rather than one sentence,
    # and its numbers are checked by the rules of the series itself.
    return ()


def _numbers(
    *values: Any,
) -> tuple[float, ...]:
    return tuple(item for item in (_number(value) for value in values) if item is not None)


def _source_and_quote(
    payload: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Read the source of a field from either supported output shape.

    The delivered file stores the source directly, while the pipeline
    output wraps it in an evidence object that also carries the quoted
    source text.
    """

    if not isinstance(payload, dict):
        return (
            None,
            None,
        )

    inner = payload.get("source")

    if isinstance(inner, dict):
        quote = payload.get("quote")

        return (
            inner,
            str(quote) if quote else None,
        )

    return (
        payload,
        None,
    )


@dataclass(frozen=True, slots=True)
class _FieldContext:
    fund_id: str
    fund_name: str
    fund_web: str | None
    field: str
    source_url: str | None
    source_type: SourceType
    source_date: str | None
    quote: str | None = None
    scope_type: str | None = None
    page: int | None = None
    confidence: str | None = None
    review_required: bool | None = None
    today: date = date(1970, 1, 1)
    carries_evidence: bool = False


def classify_source(
    *,
    source_url: str | None,
    fund_web: str | None,
) -> SourceType:
    """Classify a source relative to the fund it describes."""

    if not source_url:
        return SourceType.NONE

    source_host = canonical_domain(source_url)

    if not source_host:
        return SourceType.NONE

    fund_host = canonical_domain(fund_web) if fund_web else ""

    if fund_host and source_host == fund_host:
        return SourceType.OFFICIAL_WEBSITE

    if any(fragment in source_host for fragment in MANAGER_HOST_FRAGMENTS):
        return SourceType.MANAGER_OR_ADMINISTRATOR

    for excluded in EXCLUDED_DOMAINS:
        if source_host == excluded or source_host.endswith(f".{excluded}"):
            return SourceType.THIRD_PARTY

    return SourceType.MANAGER_OR_ADMINISTRATOR


def _finding(
    *,
    context: _FieldContext,
    status: AuditStatus,
    reason_code: str,
    reason: str,
    recommended_action: str,
    extracted_value: Any = None,
    normalized_value: str | None = None,
    evidence: str | None = None,
    related_funds: Iterable[str] = (),
) -> AuditFinding:
    return AuditFinding(
        fund_id=context.fund_id,
        fund_name=context.fund_name,
        field=context.field,
        status=status,
        severity=_STATUS_SEVERITY[status],
        reason_code=reason_code,
        reason=reason,
        recommended_action=recommended_action,
        extracted_value=extracted_value,
        normalized_value=normalized_value,
        source_url=context.source_url,
        source_type=context.source_type,
        source_date=context.source_date,
        source_scope=context.scope_type,
        evidence=(evidence or context.quote),
        related_funds=sorted(related_funds),
    )


_STATUS_SEVERITY: Final[dict[AuditStatus, ValidationSeverity]] = {
    AuditStatus.VALID: ValidationSeverity.REVIEW,
    AuditStatus.MISSING: ValidationSeverity.REVIEW,
    AuditStatus.SUSPICIOUS: ValidationSeverity.REVIEW,
    AuditStatus.CONFLICTING: ValidationSeverity.CONFLICT,
    AuditStatus.REJECTED: ValidationSeverity.REJECT,
}


def _audit_source_evidence(
    *,
    context: _FieldContext,
    shared_sources: dict[str, set[str]],
) -> list[AuditFinding]:
    """
    Check who published the source, and for how many funds it is used.

    Whether the value is traceable at all is checked by the shared
    provenance rules; what is left here needs the whole delivered file,
    because one document standing behind two funds can only be seen by
    looking at both.
    """

    findings: list[AuditFinding] = []

    if not context.source_url:
        return findings

    if context.source_type is SourceType.THIRD_PARTY:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.THIRD_PARTY_SOURCE.value,
                reason=(
                    "The value comes from an aggregator or third-party database "
                    "rather than the fund, its manager or its administrator."
                ),
                recommended_action=(
                    "Replace the value with one from the official fund or administrator source."
                ),
            )
        )

    other_funds = shared_sources.get(context.source_url, set()) - {context.fund_name}

    if other_funds:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.CONFLICTING,
                reason_code=ValidationCode.SOURCE_SHARED_ACROSS_FUNDS.value,
                reason=(
                    f"The same source document is used for {len(other_funds) + 1} "
                    "different funds, so at least one attribution must be wrong."
                ),
                recommended_action=(
                    "Verify which fund the document describes and re-extract the others."
                ),
                related_funds=other_funds,
            )
        )

    if _source_url_lacks_fund_name(
        fund_name=context.fund_name,
        source_url=context.source_url,
        source_type=context.source_type,
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.SOURCE_DOES_NOT_NAME_THE_FUND.value,
                reason=(
                    "The source is hosted by a manager or administrator and its "
                    "URL contains no distinctive word of the fund name, so the "
                    "value may belong to a different fund or subfund."
                ),
                recommended_action=(
                    "Confirm the document is issued for this exact fund or subfund."
                ),
            )
        )

    return findings


def _source_url_lacks_fund_name(
    *,
    fund_name: str,
    source_url: str,
    source_type: SourceType,
) -> bool:
    if source_type is not SourceType.MANAGER_OR_ADMINISTRATOR:
        return False

    tokens = distinctive_name_tokens(fund_name)

    if not tokens:
        return False

    normalized_url = normalize_search_text(unquote(source_url))

    return not any(token in normalized_url for token in tokens)


def _audit_investment_horizon(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that a recommended horizon is a duration of realistic length."""

    parsed = _parsed(
        InvestmentHorizonValue,
        value,
    )

    if parsed is None:
        return _malformed(
            context=context,
            value=value,
            subject="investment horizon",
        )

    return _from_validation(
        context=context,
        findings=[
            *validate_investment_horizon(parsed),
            *validate_horizon_wording(
                parsed,
                quote=context.quote,
            ),
        ],
        value=value,
        normalized_value=f"{parsed.recommended_years:g} years",
    )


def _audit_minimum_investment(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that a minimum investment is a real subscription amount."""

    parsed = _parsed(
        MinimumInvestmentValue,
        value,
    )

    if parsed is None:
        # The model refuses an inferred value without a legal basis, so a
        # payload that fails to parse for exactly that reason is reported
        # under its own rule rather than as an unreadable shape.
        if (
            isinstance(value, dict)
            and value.get("origin") == "inferred"
            and not value.get("inference")
        ):
            return [
                _finding(
                    context=context,
                    status=AuditStatus.REJECTED,
                    reason_code=(ValidationCode.INFERRED_MINIMUM_WITHOUT_BASIS.value),
                    reason=(
                        "The minimum investment is marked as inferred and records no legal basis."
                    ),
                    recommended_action=(
                        "Record the legal basis of the inference, or report "
                        "the field as not disclosed."
                    ),
                    extracted_value=value,
                )
            ]

        return _malformed(
            context=context,
            value=value,
            subject="minimum investment",
        )

    return _from_validation(
        context=context,
        findings=validate_minimum_investment(
            parsed,
            quote=context.quote,
        ),
        value=value,
        normalized_value=f"{parsed.amount:,.2f} {parsed.currency}",
    )


def _audit_target_return(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that a stated percentage really is a forward-looking return."""

    parsed = _parsed(
        TargetReturnValue,
        value,
    )

    if parsed is None:
        # The model refuses an inverted range outright, so a payload that
        # carries one never parses. Reporting it as an unreadable shape
        # would hide what is actually wrong with it.
        bounds = _numbers(
            value.get("minimum_percent_pa") if isinstance(value, dict) else None,
            value.get("maximum_percent_pa") if isinstance(value, dict) else None,
        )

        if len(bounds) == 2 and bounds[0] > bounds[1]:
            return [
                _finding(
                    context=context,
                    status=AuditStatus.CONFLICTING,
                    reason_code=(ValidationCode.INVERTED_TARGET_RETURN_RANGE.value),
                    reason=(
                        f"The range minimum {bounds[0]:g}% exceeds the maximum {bounds[1]:g}%."
                    ),
                    recommended_action="Re-extract the range boundaries.",
                    extracted_value=value,
                    normalized_value=f"{bounds[0]:g}-{bounds[1]:g} % p.a.",
                )
            ]

        return _malformed(
            context=context,
            value=value,
            subject="target return",
        )

    stated = [
        item
        for item in (
            parsed.value_percent_pa,
            parsed.minimum_percent_pa,
            parsed.maximum_percent_pa,
        )
        if item is not None
    ]

    return _from_validation(
        context=context,
        findings=[
            *validate_target_return(
                parsed,
                quote=context.quote,
                source_url=context.source_url,
            ),
            *validate_return_wording(
                parsed,
                quote=context.quote,
            ),
        ],
        value=value,
        normalized_value=" / ".join(f"{item:g} % p.a." for item in stated),
    )


def _audit_fees(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    items = value.get("items") if isinstance(value, dict) else None

    if not isinstance(items, list) or not items:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.MISSING_VALUE_COMPONENT.value,
                reason="The fee collection contains no fee item.",
                recommended_action="Re-extract the fees or mark the field as not found.",
                extracted_value=value,
            )
        ]

    findings: list[AuditFinding] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        findings.extend(
            _audit_fee_item(
                context=context,
                item=item,
            )
        )

    return findings


def _audit_fee_item(
    *,
    context: _FieldContext,
    item: dict[str, Any],
) -> list[AuditFinding]:
    """
    Check one fee: first what it says, then whether its source says it.

    The value rules are the shared ones, so a fee refused during
    extraction and a fee doubted after delivery carry the same code. What
    stays here needs the quoted line, which only the delivered file has.
    """

    fee_type = str(item.get("type") or "unknown")

    rate = _number(item.get("rate_percent"))

    fixed_amount = _number(item.get("fixed_amount"))

    basis = str(item.get("basis") or "")

    normalized_basis = normalize_search_text(basis)

    if rate is None and fixed_amount is None and not item.get("tiers"):
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.MISSING_VALUE_COMPONENT.value,
                reason=f"The {fee_type} fee has neither a rate nor a fixed amount.",
                recommended_action="Re-extract the fee value.",
                extracted_value=item,
                evidence=basis or None,
            )
        ]

    parsed = _parsed(
        FeeItem,
        item,
    )

    findings: list[AuditFinding] = []

    if parsed is None:
        findings.extend(
            _malformed(
                context=context,
                value=item,
                subject="fee",
            )
        )
    else:
        findings.extend(
            [
                finding.model_copy(update={"extracted_value": item, "evidence": basis or None})
                for finding in _from_validation(
                    context=context,
                    findings=validate_fee_items([parsed]),
                    value=item,
                    normalized_value=(f"{fee_type} {rate:g} %" if rate is not None else None),
                )
            ]
        )

    # A zero fee reaches the output written either way, and the delivered
    # data holds far more zero amounts than zero rates: thirty-one fee
    # items carry a fixed amount of zero with no rate at all, and none of
    # them was ever checked against its source line.
    if rate == 0 or fixed_amount == 0:
        findings.extend(
            _audit_zero_fee(
                context=context,
                item=item,
                fee_type=fee_type,
                basis=basis,
                normalized_basis=normalized_basis,
            )
        )

    if rate is not None and basis:
        findings.extend(
            _audit_fee_evidence(
                context=context,
                item=item,
                fee_type=fee_type,
                rate=rate,
                basis=basis,
                normalized_basis=normalized_basis,
            )
        )

    return findings


def _audit_zero_fee(
    *,
    context: _FieldContext,
    item: dict[str, Any],
    fee_type: str,
    basis: str,
    normalized_basis: str,
) -> list[AuditFinding]:
    """Check that a fee reported as zero is the fee the source states."""

    normalized_value = f"{fee_type} 0 %"

    if not basis:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.ZERO_FEE_WITHOUT_EVIDENCE.value,
                reason=f"The {fee_type} fee is zero and no source text was kept.",
                recommended_action="Re-extract the fee and store the source line.",
                extracted_value=item,
                normalized_value=normalized_value,
            )
        ]

    if any(marker in normalized_basis for marker in FEE_RANGE_MARKERS):
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.ZERO_FEE_FROM_A_RANGE.value,
                reason=(
                    f"The {fee_type} fee is reported as zero, but the source "
                    "line states a range, so the lower bound was taken as "
                    "the fee instead of the maximum."
                ),
                recommended_action=(
                    "Store the maximum of the range, or the range itself, instead of zero."
                ),
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        ]

    if not any(marker in normalized_basis for marker in FEE_ZERO_MARKERS):
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=(ValidationCode.ZERO_FEE_NOT_SUPPORTED_BY_SOURCE.value),
                reason=(
                    f"The {fee_type} fee is zero, but the source line does "
                    "not state a zero fee explicitly."
                ),
                recommended_action="Re-read the source line and correct the fee.",
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        ]

    # A key information document states each zero on its own row. When
    # only one row was captured, every fee type read out of it inherits
    # that zero: one fund's entry row "Naklady na vstup ... zadny vstupni
    # poplatek 0 EUR" produced a zero entry fee, which is right, and a
    # zero performance fee, which the row never mentions.
    labels = _FEE_TYPE_LABELS.get(fee_type)

    if labels and not any(label in normalized_basis for label in labels):
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.ZERO_FEE_OF_ANOTHER_FEE_TYPE.value,
                reason=(
                    f"The source line states a zero fee but never names a "
                    f"{fee_type} fee, so the zero was read from the row of a "
                    "different fee."
                ),
                recommended_action=("Read each fee of the cost table from its own row."),
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        ]

    return []


# The wording each fee type is named by, taken from the extraction
# vocabulary so the audit and the parser recognise the same labels.
_FEE_TYPE_LABELS: Final[dict[str, tuple[str, ...]]] = {
    fee_type.value: labels for fee_type, labels in FEE_KEYWORDS
}


def _audit_fee_evidence(
    *,
    context: _FieldContext,
    item: dict[str, Any],
    fee_type: str,
    rate: float,
    basis: str,
    normalized_basis: str,
) -> list[AuditFinding]:
    """
    Check that the stored source line really supports the fee rate.

    Magnitude alone does not separate a real fee from a parsing artefact:
    a 95 per cent exit fee is genuine, while a 100 per cent entry fee is
    usually a sentence about who receives the income. What separates them
    is how the number sits in the source line.
    """

    findings: list[AuditFinding] = []

    normalized_value = f"{fee_type} {rate:g} %"

    if not _basis_contains_number(
        basis=normalized_basis,
        number=rate,
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.VALUE_NOT_PRESENT_IN_EVIDENCE.value,
                reason=(
                    f"The {fee_type} rate {rate:g} % does not appear in the "
                    "stored source line, so the evidence does not support it."
                ),
                recommended_action="Re-extract the fee from a clean source line.",
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        )

        return findings

    distance = _label_value_distance(
        normalized_basis=normalized_basis,
        rate=rate,
    )

    # A statute writes "od 0 % do 3 % z vyse investice ... Vstupni
    # poplatek" in interleaved columns. The stored rate is the maximum of
    # that range, so the value is right and only the label is displaced.
    stores_range_maximum = _is_range_maximum(
        normalized_basis=normalized_basis,
        rate=rate,
    )

    if (
        distance is not None
        and distance > FEE_LABEL_VALUE_MAXIMUM_DISTANCE
        and not stores_range_maximum
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.VALUE_FAR_FROM_FEE_LABEL.value,
                reason=(
                    f"The {fee_type} rate stands {distance} characters away from "
                    "the fee label in the source line, which happens when PDF "
                    "columns are interleaved and the number belongs elsewhere."
                ),
                recommended_action=("Re-extract the fee from a correctly ordered source line."),
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        )

    # A clause naming the recipient of a fee is normal. It only makes the
    # value doubtful when it stands between the fee label and the number,
    # because then the number belongs to that clause and not to the fee.
    # "Vykonnostni odmena cini 20 % ... je prijmem Fondu" states a real
    # fee, while "Vstupni poplatek je prijmem Spolecnosti. ... je 100 %"
    # does not.
    if _income_share_governs_value(
        normalized_basis=normalized_basis,
        rate=rate,
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.PERCENTAGE_DESCRIBES_INCOME_SHARE.value,
                reason=(
                    "The source line states how income is shared rather than "
                    "what the investor pays, so the percentage is not a fee rate."
                ),
                recommended_action="Re-extract the fee actually charged to the investor.",
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        )

    declares_maximum = any(marker in normalized_basis for marker in FEE_MAXIMUM_MARKERS)

    if declares_maximum and item.get("maximum") is not True:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.CONFLICTING,
                reason_code=ValidationCode.MAXIMUM_FLAG_CONTRADICTS_SOURCE.value,
                reason=(
                    "The source line declares the rate as an upper limit, but "
                    "the value is not marked as a maximum."
                ),
                recommended_action="Mark the fee as a maximum, or store the range.",
                extracted_value=item,
                normalized_value=normalized_value,
                evidence=basis,
            )
        )

    return findings


def _basis_states_a_range(
    normalized_basis: str,
) -> bool:
    """Return whether the quoted fee text describes a range of rates."""

    if normalized_basis.count("%") < 2:
        return False

    return any(marker in normalized_basis for marker in FEE_RANGE_MARKERS)


def _is_range_maximum(
    *,
    normalized_basis: str,
    rate: float,
) -> bool:
    """Return whether the rate is the upper bound of a range in the text."""

    for match in FEE_RANGE_BOUNDS_PATTERN.finditer(normalized_basis):
        try:
            maximum = float(match.group("maximum").replace(",", "."))
        except ValueError:
            continue

        if abs(maximum - rate) < 0.001:
            return True

    return False


def _income_share_governs_value(
    *,
    normalized_basis: str,
    rate: float,
) -> bool:
    """Return whether an income-share clause separates the label and value."""

    label_position = min(
        (
            position
            for position in (normalized_basis.find(keyword) for keyword in FEE_LABEL_KEYWORDS)
            if position >= 0
        ),
        default=-1,
    )

    value_position = _value_position(
        normalized_basis=normalized_basis,
        rate=rate,
    )

    if value_position < 0:
        return False

    marker_position = min(
        (
            position
            for position in (normalized_basis.find(marker) for marker in FEE_INCOME_SHARE_MARKERS)
            if position >= 0
        ),
        default=-1,
    )

    if marker_position < 0:
        return False

    if label_position < 0:
        return marker_position < value_position

    if label_position < marker_position < value_position:
        return True

    # The clause reads the other way round as often as it reads this
    # way. "Vystupni poplatek (srazka) je 100 % prijmem do fondu" says
    # the whole fee goes to the fund, not that the fee takes the whole
    # investment, and the marker follows the number instead of leading
    # it. Only a marker standing directly after the value counts, so
    # "Vykonnostni odmena cini 20 % ... je prijmem Fondu", which states
    # a real fee and mentions its recipient later, keeps passing.
    value_end = value_position + len(_value_text(normalized_basis, rate))

    return label_position < value_position and 0 <= marker_position - value_end <= 3


def _value_text(
    normalized_basis: str,
    rate: float,
) -> str:
    """Return the number as it is written in the source line."""

    plain = f"{rate:g}"

    if normalized_basis.find(plain) >= 0:
        return plain

    return plain.replace(".", ",")


def _value_position(
    *,
    normalized_basis: str,
    rate: float,
) -> int:
    value_text = f"{rate:g}"

    position = normalized_basis.find(value_text)

    if position < 0:
        position = normalized_basis.find(value_text.replace(".", ","))

    return position


def _label_value_distance(
    *,
    normalized_basis: str,
    rate: float,
) -> int | None:
    """Return how far the rate stands from the nearest fee label."""

    label_positions = [
        normalized_basis.find(keyword)
        for keyword in FEE_LABEL_KEYWORDS
        if normalized_basis.find(keyword) >= 0
    ]

    if not label_positions:
        return None

    value_text = f"{rate:g}"

    value_position = normalized_basis.find(value_text)

    if value_position < 0:
        value_position = normalized_basis.find(value_text.replace(".", ","))

    if value_position < 0:
        return None

    return min(abs(value_position - position) for position in label_positions)


def _audit_assets_under_management(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that a delivered assets figure describes this fund's assets."""

    if isinstance(value, dict) and not value.get("as_of"):
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.MISSING_AS_OF_DATE.value,
                reason="Assets under management without a date cannot be interpreted.",
                recommended_action="Re-extract the value together with its reporting date.",
                extracted_value=value,
            )
        ]

    parsed = _parsed(
        AssetsUnderManagementValue,
        value,
    )

    if parsed is None:
        return _malformed(
            context=context,
            value=value,
            subject="assets under management",
        )

    normalized = f"{parsed.amount:,.0f} {parsed.currency} ({parsed.metric_type.value})"

    findings = _from_validation(
        context=context,
        findings=[
            *validate_assets_under_management(
                parsed,
                today=context.today,
            ),
            *validate_capital_attribution(
                amounts=[parsed.amount],
                quote=context.quote,
                fund_name=context.fund_name,
            ),
            *validate_capital_metric_wording(
                metric_type=parsed.metric_type,
                quote=context.quote,
            ),
        ],
        value=value,
        normalized_value=normalized,
    )

    return [
        _explained_small_amount(
            context=context,
            finding=finding,
        )
        for finding in findings
    ]


# The two ways a fund-sized amount can come out too small, and what each
# of them tells a reader to do about it.
def _explained_small_amount(
    *,
    context: _FieldContext,
    finding: AuditFinding,
) -> AuditFinding:
    """
    Name the cause of an implausibly small amount, when it can be named.

    A statement published "v tis. Kc" and a value picked from the wrong
    row of the right document both produce a number a thousand times too
    small. What separates them is whether the unit stands in the quote:
    if it does, the conversion was faithful and the wrong figure was
    chosen.
    """

    if finding.reason_code != ValidationCode.IMPLAUSIBLY_SMALL_AUM.value:
        return finding

    unit_in_evidence = context.quote is not None and any(
        marker in normalize_search_text(context.quote) for marker in AUM_UNIT_MARKERS
    )

    if unit_in_evidence:
        return finding.model_copy(
            update={
                "reason_code": (ValidationCode.AUM_VALUE_NOT_TIED_TO_LABEL.value),
                "reason": (
                    finding.reason + " The amount was converted with the unit stated next to "
                    "it, so a different amount of the document was selected "
                    "instead of the fund assets."
                ),
                "recommended_action": (
                    "Select the amount stated next to the assets label of the document."
                ),
            }
        )

    from_annual_report = context.source_url is not None and _url_has_marker(
        url=context.source_url,
        markers=ANNUAL_REPORT_URL_MARKERS,
    )

    if from_annual_report:
        return finding.model_copy(
            update={
                "reason_code": (ValidationCode.THOUSANDS_UNIT_NOT_APPLIED.value),
                "recommended_action": (
                    "Apply the unit stated in the statement header and re-extract."
                ),
            }
        )

    return finding


def _from_validation(
    *,
    context: _FieldContext,
    findings: Iterable[ValidationFinding],
    value: Any,
    normalized_value: str | None = None,
) -> list[AuditFinding]:
    """
    Turn the shared validation rules into audit findings.

    The extraction refuses a value on the same rules. Reporting them here
    with the same codes means a reviewer reads one vocabulary, whether a
    defect was caught before delivery or after it.
    """

    return [
        _finding(
            context=context,
            status=_SEVERITY_STATUS[finding.severity],
            reason_code=finding.code.value,
            reason=finding.detail,
            recommended_action=_VALIDATION_ACTIONS.get(
                finding.code.value,
                "Re-extract the field and review the source.",
            ),
            extracted_value=value,
            normalized_value=normalized_value,
        )
        for finding in findings
    ]


def _parsed[ModelT: BaseModel](
    model: type[ModelT],
    value: Any,
) -> ModelT | None:
    """Read one delivered value into its model, or report it unreadable."""

    try:
        return model.model_validate(value)
    except Exception:
        return None


_VALIDATION_ACTIONS: Final[dict[str, str]] = {
    "missing_source": ("Re-extract the field and store the source document of the value."),
    "missing_source_date": ("Store the retrieval timestamp together with the source."),
    "missing_evidence": ("Store the sentence or table row the value was read from."),
    "missing_page_reference": (
        "Record the page the quote stands on, so a reader can open the document at it."
    ),
    "evidence_does_not_support_value": (
        "Re-extract the value from a source line that contains it, or drop it."
    ),
    "scope_mismatch": ("Confirm which fund, subfund or class the value belongs to."),
    "implausible_horizon": "Check whether a different number was captured.",
    "calendar_year_as_horizon": (
        "Discard the value: a year is not a holding period. Re-extract the recommended horizon."
    ),
    "zero_minimum_investment": (
        "Discard the value and re-extract from the statute or the key information document."
    ),
    "implausibly_small_minimum_investment": (
        "Re-extract the amount and check the unit used in the source."
    ),
    "below_qualified_investor_threshold": (
        "Confirm against the statute whether this is the entry minimum."
    ),
    "non_round_minimum_investment": ("Re-extract the amount from the subscription terms."),
    "statutory_capital_as_minimum_investment": (
        "Discard the value: it is the capital the fund must hold, not what an investor subscribes."
    ),
    "year_captured_as_percentage": ("Discard the value and re-extract the target return."),
    "implausible_target_return": ("Discard the value and re-extract the target return."),
    "unusually_high_target_return": (
        "Confirm the source states a target return per annum, not past performance."
    ),
    "zero_target_return": ("Discard the value and re-extract the target return."),
    "inverted_target_return_range": "Re-extract the range boundaries.",
    "collapsed_target_return_range": (
        "Keep either the range or the single value, whichever the fund states."
    ),
    "kid_performance_scenario_as_target": (
        "Check whether the number is a scenario or a cost impact of the key information document."
    ),
    "unrelated_percentage_as_target_return": (
        "Discard the value: the percentage measures something other than a return."
    ),
    "historical_return_as_target_return": (
        "Report the figure as an achieved result, and re-extract the stated target."
    ),
    "implausible_fee_rate": ("Re-extract the fee and check the unit in the source."),
    "unusually_high_fee_rate": "Confirm the rate and its basis in the source.",
    "fee_amount_rate_mismatch": (
        "Store the figure in the unit the source writes it in, a rate or an amount."
    ),
    "missing_currency": ("Re-extract the amount together with its currency."),
    "unsupported_currency": ("Confirm the denomination of the value against the source."),
    "per_share_value_as_aum": (
        "Store the figure as a value per investment share and re-extract the fund capital."
    ),
    "implausibly_small_aum": ("Apply the unit stated in the statement header and re-extract."),
    "implausibly_large_aum": (
        "Check whether a unit multiplier was applied to an amount that already carried it."
    ),
    "implausible_aum_amount": ("Check whether the statement declares its amounts in thousands."),
    "thousands_unit_not_applied": ("Apply the unit stated in the statement header and re-extract."),
    "aum_value_not_tied_to_label": (
        "Select the amount stated next to the assets label of the document."
    ),
    "missing_as_of_date": ("Re-extract the value together with its reporting date."),
    "future_as_of_date": "Re-extract the reporting date from the statement.",
    "stale_as_of_date": "Extract the assets from the most recent statement.",
    "mixed_currencies_in_series": (
        "Split the series by currency, or convert them and record the rate used."
    ),
    "party_name_not_specific": ("Re-extract the full legal name of the company."),
    "party_role_mismatch": ("Store the party under the field matching its role."),
    "party_is_the_fund_itself": ("Re-extract the party from the section naming it."),
    "malformed_registration_number": ("Re-extract or drop the registration number."),
    "news_without_date": ("Read the publication date from the article page."),
    "news_from_unofficial_source": (
        "Replace the item with the announcement on the fund or manager website."
    ),
    "conflicting_values": (
        "Compare the two fields against their sources and correct the weaker one."
    ),
    "fallback_parser_review_required": (
        "Keep the value marked for review and below high confidence, or re-read the "
        "document with the primary parser."
    ),
    "mixed_metrics_in_series": (
        "Split the series so that each one reports a single measured quantity."
    ),
    "mixed_share_classes_in_series": (
        "Name the share class of every series, or drop the series that cannot be attributed."
    ),
    "duplicate_dates_in_series": (
        "Keep one observation per date and record which source it was taken from."
    ),
    "conflicting_annual_returns": (
        "Compare the reporting sources and keep the result of the more recent audited one."
    ),
    "cumulative_as_annual_return": (
        "Move the figure to a cumulative series, or drop it from the annual results."
    ),
    "kid_scenario_as_annual_return": (
        "Remove the performance scenario: it is a projection, not an achieved result."
    ),
    "statutory_capital_as_aum": (
        "Store the figure under its own capital metric and re-extract the assets of the fund."
    ),
    "manager_aum_as_fund_aum": (
        "Store the figure as manager-level assets and re-extract the assets of this fund."
    ),
    "news_of_another_fund": (
        "Drop the item, or confirm from the article itself that it concerns this fund."
    ),
    "inferred_minimum_without_basis": (
        "Record the legal basis of the inference, or report the field as not disclosed."
    ),
    "collapsed_fee_range": ("Store both bounds of the range, or mark the value as an upper limit."),
}


def _audit_party(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that a manager or administrator names a real company."""

    if not isinstance(value, dict):
        return []

    name = str(value.get("name") or "").strip()

    if not name:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=ValidationCode.MISSING_VALUE_COMPONENT.value,
                reason=f"The {context.field} carries no company name.",
                recommended_action="Re-extract the party from its statute.",
                extracted_value=value,
            )
        ]

    ico = value.get("ico")

    if ico is not None and not re.fullmatch(
        r"\d{8}",
        str(ico),
    ):
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=(ValidationCode.MALFORMED_REGISTRATION_NUMBER.value),
                reason=f"The registration number {ico!r} is not eight digits.",
                recommended_action="Re-extract or drop the registration number.",
                extracted_value=value,
            )
        ]

    parsed = _parsed(
        FundParty,
        value,
    )

    if parsed is None:
        return _malformed(
            context=context,
            value=value,
            subject=context.field,
        )

    return _from_validation(
        context=context,
        findings=validate_party(
            party=parsed,
            expected_role=_EXPECTED_ROLES[context.field],
            fund_name=context.fund_name,
        ),
        value=value,
        normalized_value=name,
    )


_EXPECTED_ROLES: Final[dict[str, PartyRole]] = {
    "manager": PartyRole.MANAGER,
    "administrator": PartyRole.ADMINISTRATOR,
}


def audit_cross_fields(
    *,
    fund_id: str,
    fund_name: str,
    record: dict[str, Any],
) -> list[AuditFinding]:
    """
    Check the relations between the delivered fields of one fund.

    A field can be right on its own and wrong next to its neighbours: an
    assets figure that equals the value of one investment share, a target
    return that repeats a result the fund already reported. Only a rule
    holding the whole record can see it.
    """

    view = _record_view(
        fund_name=fund_name,
        record=record,
    )

    findings = validate_cross_fields(view)

    if not findings:
        return []

    audited: list[AuditFinding] = []

    for finding in findings:
        field = finding.subject or "assets_under_management"

        context = _FieldContext(
            fund_id=fund_id,
            fund_name=fund_name,
            fund_web=None,
            field=field,
            source_url=None,
            source_type=SourceType.NONE,
            source_date=None,
        )

        audited.append(
            _finding(
                context=context,
                status=_SEVERITY_STATUS[finding.severity],
                reason_code=finding.code.value,
                reason=finding.detail,
                recommended_action=_VALIDATION_ACTIONS.get(
                    finding.code.value,
                    "Compare the two fields against their sources and correct the weaker one.",
                ),
            )
        )

    return audited


def _record_view(
    *,
    fund_name: str,
    record: dict[str, Any],
) -> FundRecordView:
    """Read the delivered values of one fund into the cross-field input."""

    return FundRecordView(
        fund_name=fund_name,
        assets_under_management=_found(
            AssetsUnderManagementValue,
            record.get("assets_under_management"),
        ),
        aum_observations=tuple(
            _found_items(
                CapitalObservation,
                record.get("aum_history"),
                "observations",
            )
        ),
        historical_series=tuple(
            _found_items(
                HistoricalValueSeries,
                record.get("historical_values"),
                "series",
            )
        ),
        annual_returns=tuple(
            _found_items(
                AnnualReturnObservation,
                record.get("annual_returns"),
                "observations",
            )
        ),
        target_return=_found(
            TargetReturnValue,
            record.get("target_return"),
        ),
        minimum_investment=_found(
            MinimumInvestmentValue,
            record.get("minimum_investment"),
        ),
        manager=_found(
            FundParty,
            record.get("manager"),
        ),
        administrator=_found(
            FundParty,
            record.get("administrator"),
        ),
    )


def _found[ModelT: BaseModel](
    model: type[ModelT],
    payload: Any,
) -> ModelT | None:
    """Return the value of a found field, or nothing at all."""

    if not isinstance(payload, dict) or payload.get("status") != "found":
        return None

    return _parsed(
        model,
        payload.get("value"),
    )


def _found_items[ModelT: BaseModel](
    model: type[ModelT],
    payload: Any,
    key: str,
) -> list[ModelT]:
    """Return the items a found collection field carries."""

    if not isinstance(payload, dict) or payload.get("status") != "found":
        return []

    value = payload.get("value")

    if not isinstance(value, dict):
        return []

    items = value.get(key)

    if not isinstance(items, list):
        return []

    parsed = [_parsed(model, item) for item in items]

    return [item for item in parsed if item is not None]


def _audit_aum_history(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check a history of fund assets against the capital rules."""

    observations = _payload_list(
        value,
        "observations",
    )

    if observations is None:
        return _malformed(
            context=context,
            value=value,
            subject="assets history",
        )

    parsed: list[CapitalObservation] = []

    for item in observations:
        observation = _parsed(
            CapitalObservation,
            item,
        )

        if observation is None:
            return _malformed(
                context=context,
                value=item,
                subject="capital observation",
            )

        parsed.append(observation)

    return [
        _explained_small_amount(
            context=context,
            finding=finding,
        )
        for finding in _from_validation(
            context=context,
            findings=[
                *validate_capital_observations(
                    parsed,
                    today=context.today,
                ),
                *validate_capital_attribution(
                    amounts=[observation.amount for observation in parsed],
                    quote=context.quote,
                    fund_name=context.fund_name,
                ),
            ],
            value=value,
        )
    ]


def _audit_annual_returns(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check reported calendar-year results against the return rules."""

    observations = _payload_list(
        value,
        "observations",
    )

    if observations is None:
        return _malformed(
            context=context,
            value=value,
            subject="annual returns",
        )

    parsed: list[AnnualReturnObservation] = []

    for item in observations:
        try:
            parsed.append(AnnualReturnObservation.model_validate(item))
        except Exception:
            return _malformed(
                context=context,
                value=item,
                subject="annual return",
            )

    return _from_validation(
        context=context,
        findings=validate_annual_returns(parsed),
        value=value,
    )


def _audit_historical_values(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that every value series measures one thing consistently."""

    raw_series = _payload_list(
        value,
        "series",
    )

    if raw_series is None:
        return _malformed(
            context=context,
            value=value,
            subject="value series",
        )

    parsed: list[HistoricalValueSeries] = []

    for item in raw_series:
        try:
            parsed.append(HistoricalValueSeries.model_validate(item))
        except Exception:
            return _malformed(
                context=context,
                value=item,
                subject="value series",
            )

    return [
        _explained_small_amount(
            context=context,
            finding=finding,
        )
        for finding in _from_validation(
            context=context,
            findings=validate_historical_series(
                parsed,
                today=context.today,
            ),
            value=value,
        )
    ]


def _audit_news(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    """Check that every news item belongs to this fund."""

    items = _payload_list(
        value,
        "items",
    )

    if items is None:
        return _malformed(
            context=context,
            value=value,
            subject="news",
        )

    parsed: list[FundNewsItem] = []

    for item in items:
        try:
            parsed.append(FundNewsItem.model_validate(item))
        except Exception:
            return _malformed(
                context=context,
                value=item,
                subject="news item",
            )

    return _from_validation(
        context=context,
        findings=validate_news_items(
            items=parsed,
            fund_name=context.fund_name,
            fund_web=context.fund_web,
        ),
        value=value,
    )


def _payload_list(
    value: Any,
    key: str,
) -> list[Any] | None:
    """Return the list a collection field wraps, or None when malformed."""

    if not isinstance(value, dict):
        return None

    items = value.get(key)

    if not isinstance(items, list) or not items:
        return None

    return items


def _malformed(
    *,
    context: _FieldContext,
    value: Any,
    subject: str,
) -> list[AuditFinding]:
    return [
        _finding(
            context=context,
            status=AuditStatus.SUSPICIOUS,
            reason_code=ValidationCode.MALFORMED_VALUE.value,
            reason=f"The {subject} does not match the shape the schema defines.",
            recommended_action="Re-extract the field with the current extraction rules.",
            extracted_value=value,
        )
    ]


_FIELD_CHECKERS: Final[dict[str, Any]] = {
    "investment_horizon": _audit_investment_horizon,
    "minimum_investment": _audit_minimum_investment,
    "target_return": _audit_target_return,
    "fees": _audit_fees,
    "assets_under_management": _audit_assets_under_management,
    "manager": _audit_party,
    "administrator": _audit_party,
    "aum_history": _audit_aum_history,
    "annual_returns": _audit_annual_returns,
    "historical_values": _audit_historical_values,
    "news": _audit_news,
}


def _shared_source_urls(
    records: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Map each source URL to the funds that rely on it."""

    funds_by_url: dict[str, set[str]] = defaultdict(set)

    for record in records:
        fund_name = str(record.get("name") or "")

        for field in ALL_AUDITED_FIELDS:
            payload = record.get(field)

            if not isinstance(payload, dict) or payload.get("status") != "found":
                continue

            source = payload.get("source")

            if not isinstance(source, dict):
                continue

            url = source.get("url")

            if isinstance(url, str) and url:
                funds_by_url[url].add(fund_name)

    return {url: funds for url, funds in funds_by_url.items() if len(funds) > 1}


def _schema_notes(
    records: list[dict[str, Any]],
) -> list[str]:
    """Describe what the audited file cannot express."""

    notes: list[str] = []

    if records and "fund_id" not in records[0]:
        notes.append(
            "The file carries no fund_id. Identifiers in this report are derived "
            "from the fund name and website."
        )

    sample = records[0].get("investment_horizon") if records else None

    if isinstance(sample, dict) and "extraction" not in sample:
        notes.append(
            "The file carries no extraction metadata, so extraction method, "
            "confidence and review flags could not be audited."
        )

    if isinstance(sample, dict):
        source = sample.get("source")

        if isinstance(source, dict) and "quote" not in source:
            notes.append(
                "The file carries no quoted source text except the fee basis, so "
                "support of a value by its source text could only be audited for fees."
            )

    return notes


def _url_has_marker(
    *,
    url: str,
    markers: tuple[str, ...],
) -> bool:
    normalized = normalize_search_text(unquote(url))

    return any(marker in normalized for marker in markers)


def _basis_contains_number(
    *,
    basis: str,
    number: float,
) -> bool:
    """Return whether the source line contains the extracted number."""

    variants = {
        f"{number:g}",
        f"{number:.1f}".replace(".", ","),
        f"{number:.2f}".replace(".", ","),
        f"{number:g}".replace(".", ","),
    }

    digits = re.findall(
        r"\d+(?:[.,]\d+)?",
        basis,
    )

    normalized_digits = {item.replace(",", ".") for item in digits}

    for variant in variants:
        if variant in basis:
            return True

        if variant.replace(",", ".") in normalized_digits:
            return True

    return False


def _number(
    value: Any,
) -> float | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, int | float):
        return float(value)

    return None


def _parse_date(
    value: Any,
) -> date | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def write_audit_report(
    *,
    report: AuditReport,
    path: Path,
) -> None:
    """Write the audit report as atomic JSON."""

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

        raise OutputAuditError(f"Audit report could not be written: {path}: {exc}") from exc
