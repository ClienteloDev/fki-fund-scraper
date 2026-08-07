from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
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
    ValidationFinding,
    ValidationSeverity,
    validate_annual_returns,
    validate_capital_observations,
    validate_historical_series,
    validate_minimum_investment,
    validate_news_items,
)
from fundscraper.field_definitions import is_generic_company_name
from fundscraper.html_discovery import normalize_search_text
from fundscraper.normalization import canonical_domain
from fundscraper.output_models import (
    AnnualReturnObservation,
    CapitalObservation,
    FundNewsItem,
    HistoricalValueSeries,
    MinimumInvestmentValue,
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
    VALID = "valid"
    SUSPICIOUS = "suspicious"
    CONFLICTING = "conflicting"
    MISSING = "missing"


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


# Czech qualified investor funds have a legal entry threshold of
# 1 000 000 CZK, or 125 000 EUR together with a suitability assessment
# (ZISIF). Values far below that are almost always a parsing artefact,
# such as a nominal share price or a unit value.
MINIMUM_INVESTMENT_IMPLAUSIBLE: Final[dict[str, float]] = {
    "CZK": 1_000.0,
    "EUR": 100.0,
    "USD": 100.0,
}

MINIMUM_INVESTMENT_BELOW_THRESHOLD: Final[dict[str, float]] = {
    "CZK": 100_000.0,
    "EUR": 5_000.0,
    "USD": 5_000.0,
}


# A fund of any size holds far more than this. Smaller reported assets
# almost always mean a thousands unit of a Czech annual report was not
# applied, because those statements are published "v tis. Kc".
AUM_IMPLAUSIBLE_AMOUNT: Final[float] = 1_000_000.0

AUM_MAXIMUM_AGE_DAYS: Final[int] = 5 * 365


# Target returns of qualified investor funds cluster between 4 and 15 per
# cent a year. Higher figures are usually a cumulative or historical
# performance, or a KID performance scenario, rather than a target.
TARGET_RETURN_HIGH: Final[float] = 25.0
TARGET_RETURN_IMPLAUSIBLE: Final[float] = 100.0
TARGET_RETURN_KID_SUSPICIOUS: Final[float] = 15.0

YEAR_LIKE_MINIMUM: Final[float] = 1_900.0
YEAR_LIKE_MAXIMUM: Final[float] = 2_100.0


HORIZON_MAXIMUM_YEARS: Final[float] = 30.0


FEE_RATE_IMPLAUSIBLE: Final[float] = 100.0


# Manual review of the delivered data showed that high exit and
# performance fees are genuine in Czech qualified investor funds, for
# example a 95 per cent exit fee during the investment period or a 45 per
# cent performance fee above a hurdle. Only the fee types that are
# capped in practice are checked against a magnitude threshold.
FEE_RATE_HIGH_BY_TYPE: Final[dict[str, float]] = {
    "entry": 10.0,
    "management": 10.0,
    "administration": 10.0,
    "depositary": 10.0,
    "ongoing": 15.0,
    "transaction": 15.0,
}


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


# Wording that explicitly supports a zero fee.
FEE_ZERO_MARKERS: Final[tuple[str, ...]] = (
    "0 %",
    "0%",
    "0,00 %",
    "0.00 %",
    "bez poplatku",
    "zdarma",
    "no fee",
    "free of charge",
)


KID_URL_MARKERS: Final[tuple[str, ...]] = (
    "kid",
    "priips",
    "kiid",
    "sdeleni-klicovych",
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


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    input_path: str
    input_sha256: str
    schema_notes: list[str] = Field(default_factory=list)
    summary: AuditSummary
    findings: list[AuditFinding] = Field(default_factory=list)


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

    fields_checked = 0

    for record in records:
        fund_name = str(record.get("name") or "")

        fund_web = record.get("web")

        fund_id = stable_fund_identifier(
            name=fund_name,
            web=fund_web if isinstance(fund_web, str) else None,
        )

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
            )

            by_status[outcome.status.value] += 1

            by_field[field][outcome.status.value] += 1

            for finding in outcome.findings:
                by_reason[finding.reason_code] += 1

                by_source_type[finding.source_type.value] += 1

            findings.extend(outcome.findings)

    return AuditReport(
        generated_at=(now or datetime.now(UTC)),
        input_path=str(input_path),
        input_sha256=input_sha256,
        schema_notes=_schema_notes(records),
        summary=AuditSummary(
            funds=len(records),
            fields_checked=fields_checked,
            by_status=dict(sorted(by_status.items())),
            by_field={field: dict(sorted(counts.items())) for field, counts in by_field.items()},
            by_reason=dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
            by_source_type=dict(sorted(by_source_type.items())),
        ),
        findings=findings,
    )


def audit_field(
    *,
    fund_id: str,
    fund_name: str,
    fund_web: str | None,
    field: str,
    payload: Any,
    shared_sources: dict[str, set[str]],
) -> FieldOutcome:
    """Classify one field of one fund."""

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
    )

    findings: list[AuditFinding] = []

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

    if not findings:
        return FieldOutcome(
            status=AuditStatus.VALID,
            findings=(),
        )

    worst = (
        AuditStatus.CONFLICTING
        if any(finding.status is AuditStatus.CONFLICTING for finding in findings)
        else AuditStatus.SUSPICIOUS
    )

    return FieldOutcome(
        status=worst,
        findings=tuple(findings),
    )


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


def _audit_source_evidence(
    *,
    context: _FieldContext,
    shared_sources: dict[str, set[str]],
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    if not context.source_url:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_source",
                reason="A found value carries no source reference at all.",
                recommended_action=(
                    "Re-extract the field and store the source document of the value."
                ),
            )
        )

        return findings

    if not context.source_date:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_source_date",
                reason="The source has no retrieval date, so the value cannot be aged.",
                recommended_action="Store the retrieval timestamp together with the source.",
            )
        )

    if context.source_type is SourceType.THIRD_PARTY:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="third_party_source",
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
                reason_code="source_shared_across_funds",
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
                reason_code="source_does_not_name_the_fund",
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
    years = _number(value.get("recommended_years") if isinstance(value, dict) else None)

    if years is None:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_value_component",
                reason="The recommended horizon has no numeric value.",
                recommended_action="Re-extract the horizon or mark the field as not found.",
                extracted_value=value,
            )
        ]

    if years <= 0 or years > HORIZON_MAXIMUM_YEARS:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="implausible_horizon",
                reason=(
                    f"A recommended horizon of {years:g} years is outside the "
                    f"plausible range of 0 to {HORIZON_MAXIMUM_YEARS:g} years."
                ),
                recommended_action="Check whether a different number was captured.",
                extracted_value=value,
                normalized_value=f"{years:g} years",
            )
        ]

    return []


def _audit_minimum_investment(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    if not isinstance(value, dict):
        return []

    amount = _number(value.get("amount"))

    currency = str(value.get("currency") or "")

    findings: list[AuditFinding] = []

    if amount is None or not currency:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_value_component",
                reason="The minimum investment has no amount or no currency.",
                recommended_action="Re-extract the amount together with its currency.",
                extracted_value=value,
            )
        )

        return findings

    normalized = f"{amount:,.2f} {currency}"

    # A minimum the pipeline derived from law instead of reading it must
    # carry the provision it rests on. Without it a reader cannot tell an
    # inferred threshold from one the fund actually published.
    try:
        findings.extend(
            _from_validation(
                context=context,
                findings=validate_minimum_investment(MinimumInvestmentValue.model_validate(value)),
                value=value,
            )
        )
    except Exception:
        if value.get("origin") == "inferred" and not value.get("inference"):
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.CONFLICTING,
                    reason_code="inferred_minimum_without_basis",
                    reason=(
                        "The minimum investment is marked as inferred and records no legal basis."
                    ),
                    recommended_action=(
                        "Record the legal basis of the inference, or report "
                        "the field as not disclosed."
                    ),
                    extracted_value=value,
                    normalized_value=normalized,
                )
            )

    if amount == 0:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="zero_minimum_investment",
                reason="A minimum investment of zero cannot be a real subscription limit.",
                recommended_action="Discard the value and re-extract from the statute or KID.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )
    elif amount < MINIMUM_INVESTMENT_IMPLAUSIBLE.get(currency, 0.0):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="implausibly_small_minimum_investment",
                reason=(
                    f"{normalized} is far below any realistic subscription and "
                    "usually indicates a nominal share price, a unit value or a "
                    "lost thousands multiplier."
                ),
                recommended_action=("Re-extract the amount and check the unit used in the source."),
                extracted_value=value,
                normalized_value=normalized,
            )
        )
    elif amount < MINIMUM_INVESTMENT_BELOW_THRESHOLD.get(currency, 0.0):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="below_qualified_investor_threshold",
                reason=(
                    f"{normalized} is well below the qualified investor entry "
                    "threshold, so it may describe a savings plan instalment "
                    "or a different fee rather than the minimum investment."
                ),
                recommended_action=(
                    "Confirm against the statute whether this is the entry minimum."
                ),
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    if amount and amount != round(amount):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="non_round_minimum_investment",
                reason=(
                    f"{normalized} is not a round subscription amount and looks "
                    "like a unit price or an exchange rate."
                ),
                recommended_action="Re-extract the amount from the subscription terms.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    return findings


def _audit_target_return(
    context: _FieldContext,
    value: Any,
) -> list[AuditFinding]:
    if not isinstance(value, dict):
        return []

    exact = _number(value.get("value_percent_pa"))

    minimum = _number(value.get("minimum_percent_pa"))

    maximum = _number(value.get("maximum_percent_pa"))

    findings: list[AuditFinding] = []

    if exact is None and minimum is None and maximum is None:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_value_component",
                reason="The target return contains neither an exact value nor a range.",
                recommended_action="Re-extract the target return or mark it as not found.",
                extracted_value=value,
            )
        ]

    if minimum is not None and maximum is not None and minimum > maximum:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.CONFLICTING,
                reason_code="inverted_target_return_range",
                reason=f"The range minimum {minimum:g}% exceeds the maximum {maximum:g}%.",
                recommended_action="Re-extract the range boundaries.",
                extracted_value=value,
                normalized_value=f"{minimum:g}-{maximum:g} % p.a.",
            )
        )

    candidates = [item for item in (exact, minimum, maximum) if item is not None]

    highest = max(candidates)

    normalized = " / ".join(f"{item:g} % p.a." for item in candidates)

    if any(YEAR_LIKE_MINIMUM <= item <= YEAR_LIKE_MAXIMUM for item in candidates):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="year_captured_as_percentage",
                reason=(
                    "The value lies in the range of a calendar year, so a year "
                    "was almost certainly captured instead of a percentage."
                ),
                recommended_action="Discard the value and re-extract the target return.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )
    elif highest > TARGET_RETURN_IMPLAUSIBLE:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="implausible_target_return",
                reason=(
                    f"A target return of {highest:g} % a year exceeds any realistic fund target."
                ),
                recommended_action="Discard the value and re-extract the target return.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )
    elif highest > TARGET_RETURN_HIGH:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="unusually_high_target_return",
                reason=(
                    f"{highest:g} % a year is far above the usual band of "
                    "Czech qualified investor funds and may be a cumulative or "
                    "historical performance rather than a target."
                ),
                recommended_action=(
                    "Confirm the source states a target return per annum, not past performance."
                ),
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    if exact == 0 and minimum is None and maximum is None:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="zero_target_return",
                reason="A target return of zero is not a meaningful fund target.",
                recommended_action="Discard the value and re-extract the target return.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    if (
        context.source_url
        and highest > TARGET_RETURN_KID_SUSPICIOUS
        and _url_has_marker(
            url=context.source_url,
            markers=KID_URL_MARKERS,
        )
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="kid_performance_scenario_as_target",
                reason=(
                    "The value comes from a KID or PRIIPS document and is high "
                    "enough to be a performance scenario, which is not a target return."
                ),
                recommended_action=(
                    "Check whether the number is a favourable scenario in the KID."
                ),
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    return findings


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
                reason_code="missing_value_component",
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
    fee_type = str(item.get("type") or "unknown")

    rate = _number(item.get("rate_percent"))

    fixed_amount = _number(item.get("fixed_amount"))

    currency = item.get("currency")

    basis = str(item.get("basis") or "")

    normalized_basis = normalize_search_text(basis)

    findings: list[AuditFinding] = []

    if rate is None and fixed_amount is None:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_value_component",
                reason=f"The {fee_type} fee has neither a rate nor a fixed amount.",
                recommended_action="Re-extract the fee value.",
                extracted_value=item,
                evidence=basis or None,
            )
        )

        return findings

    minimum_rate = _number(item.get("minimum_rate_percent"))

    maximum_rate = _number(item.get("maximum_rate_percent"))

    if (
        rate is not None
        and minimum_rate is None
        and maximum_rate is None
        and _basis_states_a_range(normalized_basis)
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="collapsed_fee_range",
                reason=(
                    f"The {fee_type} fee is written as a range in its "
                    f"source, but only {rate:g} % is stored."
                ),
                recommended_action=(
                    "Store both bounds of the range, or mark the value as an upper limit."
                ),
                extracted_value=item,
                evidence=basis or None,
            )
        )

    if fixed_amount is not None and not currency:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_currency",
                reason=f"The {fee_type} fee has a fixed amount without a currency.",
                recommended_action="Re-extract the amount together with its currency.",
                extracted_value=item,
                evidence=basis or None,
            )
        )

    if rate is not None and rate == 0:
        if not basis:
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.SUSPICIOUS,
                    reason_code="zero_fee_without_evidence",
                    reason=f"The {fee_type} fee is zero and no source text was kept.",
                    recommended_action="Re-extract the fee and store the source line.",
                    extracted_value=item,
                    normalized_value=f"{fee_type} 0 %",
                )
            )
        elif any(marker in normalized_basis for marker in FEE_RANGE_MARKERS):
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.SUSPICIOUS,
                    reason_code="zero_fee_from_a_range",
                    reason=(
                        f"The {fee_type} fee is reported as zero, but the source "
                        "line states a range, so the lower bound was taken as "
                        "the fee instead of the maximum."
                    ),
                    recommended_action=(
                        "Store the maximum of the range, or the range itself, instead of zero."
                    ),
                    extracted_value=item,
                    normalized_value=f"{fee_type} 0 %",
                    evidence=basis,
                )
            )
        elif not any(marker in normalized_basis for marker in FEE_ZERO_MARKERS):
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.SUSPICIOUS,
                    reason_code="zero_fee_not_supported_by_source",
                    reason=(
                        f"The {fee_type} fee is zero, but the source line does "
                        "not state a zero fee explicitly."
                    ),
                    recommended_action="Re-read the source line and correct the fee.",
                    extracted_value=item,
                    normalized_value=f"{fee_type} 0 %",
                    evidence=basis,
                )
            )

    if rate is not None and rate > FEE_RATE_IMPLAUSIBLE:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="implausible_fee_rate",
                reason=(
                    f"A {fee_type} fee of {rate:g} % is above 100 % and is "
                    "probably an absolute amount captured as a percentage."
                ),
                recommended_action="Re-extract the fee and check the unit in the source.",
                extracted_value=item,
                normalized_value=f"{fee_type} {rate:g} %",
                evidence=basis or None,
            )
        )
    elif rate is not None and rate > FEE_RATE_HIGH_BY_TYPE.get(fee_type, FEE_RATE_IMPLAUSIBLE):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="unusually_high_fee_rate",
                reason=(
                    f"A {fee_type} fee of {rate:g} % is far above the usual band "
                    f"for this fee type and may be a different figure of the source."
                ),
                recommended_action="Confirm the rate and its basis in the source.",
                extracted_value=item,
                normalized_value=f"{fee_type} {rate:g} %",
                evidence=basis or None,
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
                reason_code="value_not_present_in_evidence",
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
                reason_code="value_far_from_fee_label",
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
                reason_code="percentage_describes_income_share",
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
                reason_code="maximum_flag_contradicts_source",
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

    return label_position < marker_position < value_position


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
    if not isinstance(value, dict):
        return []

    amount = _number(value.get("amount"))

    currency = str(value.get("currency") or "")

    metric_type = str(value.get("metric_type") or "")

    as_of = value.get("as_of")

    findings: list[AuditFinding] = []

    if amount is None or not currency:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_value_component",
                reason="The assets under management have no amount or no currency.",
                recommended_action="Re-extract the amount together with its currency.",
                extracted_value=value,
            )
        )

        return findings

    normalized = f"{amount:,.0f} {currency} ({metric_type or 'unknown metric'})"

    if not as_of:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_as_of_date",
                reason="Assets under management without a date cannot be interpreted.",
                recommended_action="Re-extract the value together with its reporting date.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    if amount < AUM_IMPLAUSIBLE_AMOUNT:
        from_annual_report = context.source_url is not None and _url_has_marker(
            url=context.source_url,
            markers=ANNUAL_REPORT_URL_MARKERS,
        )

        # When the evidence already carries the unit next to the amount,
        # the number was converted faithfully and the real defect is that
        # a different amount of the document was selected.
        unit_in_evidence = context.quote is not None and any(
            marker in normalize_search_text(context.quote) for marker in AUM_UNIT_MARKERS
        )

        if unit_in_evidence:
            reason_code = "aum_value_not_tied_to_label"

            explanation = (
                "The amount was converted with the unit stated next to it, "
                "so the value is not a unit problem. A different amount of "
                "the document was selected instead of the fund assets."
            )
        elif from_annual_report:
            reason_code = "thousands_unit_not_applied"

            explanation = (
                "The source is a financial statement, which reports "
                "amounts in thousands, so the multiplier was lost."
            )
        else:
            reason_code = "implausibly_small_aum"

            explanation = "The captured number is probably not the fund assets."

        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code=reason_code,
                reason=f"{normalized} is far too small for a fund. {explanation}",
                recommended_action=(
                    "Select the amount stated next to the assets label of the document."
                    if reason_code == "aum_value_not_tied_to_label"
                    else "Apply the unit stated in the statement header and re-extract."
                ),
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    if metric_type == "manager_aum":
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="manager_level_scope",
                reason="The value describes the assets of the manager, not of this fund.",
                recommended_action="Replace it with a fund-level or subfund-level figure.",
                extracted_value=value,
                normalized_value=normalized,
            )
        )

    as_of_date = _parse_date(as_of)

    if as_of_date is not None:
        today = datetime.now(UTC).date()

        if as_of_date > today:
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.SUSPICIOUS,
                    reason_code="future_as_of_date",
                    reason=f"The reporting date {as_of_date.isoformat()} lies in the future.",
                    recommended_action="Re-extract the reporting date from the statement.",
                    extracted_value=value,
                    normalized_value=normalized,
                )
            )
        elif (today - as_of_date).days > AUM_MAXIMUM_AGE_DAYS:
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.SUSPICIOUS,
                    reason_code="stale_as_of_date",
                    reason=(
                        f"The reporting date {as_of_date.isoformat()} is older "
                        "than five years, so the value is outdated."
                    ),
                    recommended_action="Extract the assets from the most recent statement.",
                    extracted_value=value,
                    normalized_value=normalized,
                )
            )

    return findings


def _from_validation(
    *,
    context: _FieldContext,
    findings: Iterable[ValidationFinding],
    value: Any,
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
            status=(
                AuditStatus.CONFLICTING
                if finding.severity is ValidationSeverity.REJECT
                else AuditStatus.SUSPICIOUS
            ),
            reason_code=finding.code.value,
            reason=finding.detail,
            recommended_action=_VALIDATION_ACTIONS.get(
                finding.code.value,
                "Re-extract the field and review the source.",
            ),
            extracted_value=value,
        )
        for finding in findings
    ]


_VALIDATION_ACTIONS: Final[dict[str, str]] = {
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

    findings: list[AuditFinding] = []

    name = str(value.get("name") or "").strip()

    role = str(value.get("role") or "")

    if not name:
        return [
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="missing_value_component",
                reason=f"The {context.field} carries no company name.",
                recommended_action="Re-extract the party from its statute.",
                extracted_value=value,
            )
        ]

    if is_generic_company_name(name):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="party_name_not_specific",
                reason=(
                    f"The {context.field} is stored as {name!r}, which is "
                    "a legal form and names no company."
                ),
                recommended_action="Re-extract the full legal name of the company.",
                extracted_value=value,
                normalized_value=name,
            )
        )

    if role and role != context.field:
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="party_role_mismatch",
                reason=(f"The {context.field} field carries a party of role {role!r}."),
                recommended_action="Store the party under the field matching its role.",
                extracted_value=value,
            )
        )

    tokens = distinctive_name_tokens(context.fund_name)

    normalized_name = normalize_search_text(name)

    if tokens and all(token in normalized_name for token in tokens):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="party_is_the_fund_itself",
                reason=(
                    f"The {context.field} repeats the name of the fund, so "
                    "the company acting for it was not identified."
                ),
                recommended_action="Re-extract the party from the section naming it.",
                extracted_value=value,
                normalized_value=name,
            )
        )

    ico = value.get("ico")

    if ico is not None and not re.fullmatch(
        r"\d{8}",
        str(ico),
    ):
        findings.append(
            _finding(
                context=context,
                status=AuditStatus.SUSPICIOUS,
                reason_code="malformed_registration_number",
                reason=f"The registration number {ico!r} is not eight digits.",
                recommended_action="Re-extract or drop the registration number.",
                extracted_value=value,
            )
        )

    return findings


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
        try:
            parsed.append(CapitalObservation.model_validate(item))
        except Exception:
            return _malformed(
                context=context,
                value=item,
                subject="capital observation",
            )

    findings = _from_validation(
        context=context,
        findings=validate_capital_observations(parsed),
        value=value,
    )

    for observation in parsed:
        if observation.amount < AUM_IMPLAUSIBLE_AMOUNT:
            findings.append(
                _finding(
                    context=context,
                    status=AuditStatus.SUSPICIOUS,
                    reason_code="implausible_aum_amount",
                    reason=(
                        f"The value of {observation.as_of.isoformat()} is "
                        f"{observation.amount:,.0f} {observation.currency}, "
                        "which is far below what a fund of this kind holds."
                    ),
                    recommended_action=(
                        "Check whether the statement declares its amounts in thousands."
                    ),
                    extracted_value=observation.model_dump(mode="json"),
                )
            )

    return findings


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

    return _from_validation(
        context=context,
        findings=validate_historical_series(parsed),
        value=value,
    )


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
            reason_code="malformed_value",
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
