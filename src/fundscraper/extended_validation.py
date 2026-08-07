"""
Validation rules of the extended fund data set.

Every rule here names one way in which a value can be wrong while still
being well formed. A series of two different metrics validates against
the schema, a statutory minimum capital is a perfectly good amount, and
a news item of a neighbouring fund is a perfectly good article. Only a
rule that knows what the value is supposed to mean can reject them.

The rules are pure: they read a value and return findings. The extraction
uses them to refuse a defective series, and the audit uses the same rules
to explain a delivered one, so both agree on what "wrong" means.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from fundscraper.field_definitions import NON_FUND_CAPITAL_METRICS
from fundscraper.html_discovery import normalize_search_text
from fundscraper.normalization import canonical_domain
from fundscraper.output_models import (
    AnnualReturnObservation,
    AumMetricType,
    CapitalObservation,
    FeeItem,
    FundNewsItem,
    HistoricalValueSeries,
    MinimumInvestmentValue,
    NewsSourceType,
    ReturnSeriesType,
    TargetReturnValue,
    ValueOrigin,
)


class ValidationCode(StrEnum):
    """Why one extended value cannot be trusted as it stands."""

    MIXED_METRICS_IN_SERIES = "mixed_metrics_in_series"
    MIXED_SHARE_CLASSES_IN_SERIES = "mixed_share_classes_in_series"
    DUPLICATE_DATES_IN_SERIES = "duplicate_dates_in_series"
    CONFLICTING_ANNUAL_RETURNS = "conflicting_annual_returns"
    CUMULATIVE_AS_ANNUAL_RETURN = "cumulative_as_annual_return"
    KID_SCENARIO_AS_ANNUAL_RETURN = "kid_scenario_as_annual_return"
    STATUTORY_CAPITAL_AS_AUM = "statutory_capital_as_aum"
    MANAGER_AUM_AS_FUND_AUM = "manager_aum_as_fund_aum"
    NEWS_OF_ANOTHER_FUND = "news_of_another_fund"
    INFERRED_MINIMUM_WITHOUT_BASIS = "inferred_minimum_without_basis"
    COLLAPSED_FEE_RANGE = "collapsed_fee_range"


class ValidationSeverity(StrEnum):
    """Whether a finding refuses a value or only marks it for review."""

    REJECT = "reject"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: ValidationCode
    severity: ValidationSeverity
    detail: str


# A calendar-year return of a qualified investor fund lies well inside
# this band. A larger figure is a cumulative result of several years
# presented in a yearly table.
PLAUSIBLE_ANNUAL_RETURN_LIMIT: Final = 60.0


# Two reports of the same year may round differently. A larger gap means
# the two figures describe different things.
ANNUAL_RETURN_TOLERANCE: Final = 0.05


def validate_capital_observations(
    observations: Sequence[CapitalObservation],
) -> list[ValidationFinding]:
    """Check a series of capital figures reported as the fund assets."""

    findings: list[ValidationFinding] = []

    metrics = {observation.metric_type for observation in observations}

    statutory = sorted(
        metric.value for metric in metrics if metric is AumMetricType.STATUTORY_MINIMUM_CAPITAL
    )

    if statutory:
        findings.append(
            ValidationFinding(
                code=ValidationCode.STATUTORY_CAPITAL_AS_AUM,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The series contains the statutory minimum capital, "
                    "which is the floor the law requires and not what "
                    "the fund holds."
                ),
            )
        )

    if AumMetricType.MANAGER_AUM in metrics:
        findings.append(
            ValidationFinding(
                code=ValidationCode.MANAGER_AUM_AS_FUND_AUM,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The series contains assets reported for the manager "
                    "or the group, which are not the assets of this fund."
                ),
            )
        )

    if AumMetricType.REGISTERED_CAPITAL in metrics:
        findings.append(
            ValidationFinding(
                code=ValidationCode.STATUTORY_CAPITAL_AS_AUM,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The series contains the registered capital of the "
                    "fund, which is a fixed founding amount and not its "
                    "assets under management."
                ),
            )
        )

    findings.extend(
        _duplicate_date_findings(
            keys=[
                (
                    observation.metric_type.value,
                    observation.share_class or "",
                    observation.currency,
                    observation.as_of.isoformat(),
                )
                for observation in observations
            ],
            subject="capital observation",
        )
    )

    return findings


def validate_annual_returns(
    observations: Sequence[AnnualReturnObservation],
) -> list[ValidationFinding]:
    """Check a series reported as the calendar-year performance."""

    findings: list[ValidationFinding] = []

    series_types = {observation.series_type for observation in observations}

    if ReturnSeriesType.CUMULATIVE in series_types:
        findings.append(
            ValidationFinding(
                code=ValidationCode.CUMULATIVE_AS_ANNUAL_RETURN,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "A cumulative return covering several years is "
                    "reported among the calendar-year results."
                ),
            )
        )

    if ReturnSeriesType.KID_SCENARIO in series_types:
        findings.append(
            ValidationFinding(
                code=ValidationCode.KID_SCENARIO_AS_ANNUAL_RETURN,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "A performance scenario of a key information "
                    "document is reported as an achieved annual result."
                ),
            )
        )

    non_calendar = series_types - {ReturnSeriesType.CALENDAR_YEAR}

    if non_calendar - {
        ReturnSeriesType.CUMULATIVE,
        ReturnSeriesType.KID_SCENARIO,
    }:
        findings.append(
            ValidationFinding(
                code=ValidationCode.MIXED_METRICS_IN_SERIES,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The series mixes calendar-year results with "
                    f"{', '.join(sorted(item.value for item in non_calendar))}."
                ),
            )
        )

    by_year: dict[
        tuple[int, str],
        list[float],
    ] = defaultdict(list)

    for observation in observations:
        if observation.series_type is not ReturnSeriesType.CALENDAR_YEAR:
            continue

        by_year[
            (
                observation.year,
                observation.share_class or "",
            )
        ].append(observation.return_percent)

    for (year, share_class), values in sorted(by_year.items()):
        if max(values) - min(values) <= ANNUAL_RETURN_TOLERANCE:
            continue

        findings.append(
            ValidationFinding(
                code=ValidationCode.CONFLICTING_ANNUAL_RETURNS,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"Year {year}"
                    + (f" of class {share_class}" if share_class else "")
                    + " is reported with different results: "
                    + ", ".join(f"{value:g} %" for value in sorted(values))
                ),
            )
        )

    for observation in observations:
        if (
            observation.series_type is ReturnSeriesType.CALENDAR_YEAR
            and abs(observation.return_percent) > PLAUSIBLE_ANNUAL_RETURN_LIMIT
        ):
            findings.append(
                ValidationFinding(
                    code=ValidationCode.CUMULATIVE_AS_ANNUAL_RETURN,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        f"The result of {observation.year} is "
                        f"{observation.return_percent:g} %, which is far "
                        "above what one calendar year of such a fund "
                        "produces and reads as a cumulative figure."
                    ),
                )
            )

    return findings


def validate_historical_series(
    series: Sequence[HistoricalValueSeries],
) -> list[ValidationFinding]:
    """Check that every value series measures one thing consistently."""

    findings: list[ValidationFinding] = []

    identities = Counter(
        (
            item.value_type.value,
            item.share_class or "",
            item.currency,
        )
        for item in series
    )

    for identity, count in sorted(identities.items()):
        if count > 1:
            findings.append(
                ValidationFinding(
                    code=ValidationCode.MIXED_METRICS_IN_SERIES,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        "The same quantity is reported by more than one "
                        f"series: {identity[0]} of class "
                        f"{identity[1] or 'unspecified'} in {identity[2]}."
                    ),
                )
            )

    by_type: dict[str, set[str]] = defaultdict(set)

    for item in series:
        by_type[item.value_type.value].add(item.share_class or "")

    for value_type, classes in sorted(by_type.items()):
        if len(classes) > 1 and "" in classes:
            findings.append(
                ValidationFinding(
                    code=ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        f"The {value_type} series exist both with and "
                        "without a share class, so one of them may hold "
                        "the values of a class it does not name."
                    ),
                )
            )

    for item in series:
        findings.extend(
            _duplicate_date_findings(
                keys=[observation.as_of.isoformat() for observation in item.observations],
                subject=f"{item.value_type.value} series",
            )
        )

    return findings


def validate_news_items(
    *,
    items: Sequence[FundNewsItem],
    fund_name: str,
    fund_web: str | None,
) -> list[ValidationFinding]:
    """Check that every news item belongs to this fund."""

    findings: list[ValidationFinding] = []

    fund_host = canonical_domain(fund_web) if fund_web else ""

    tokens = _distinctive_tokens(fund_name)

    for item in items:
        host = canonical_domain(str(item.url))

        if fund_host and host == fund_host:
            continue

        if item.source_type is NewsSourceType.THIRD_PARTY:
            findings.append(
                ValidationFinding(
                    code=ValidationCode.NEWS_OF_ANOTHER_FUND,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        "The item comes from a third party, which this "
                        f"step does not cover: {item.url}"
                    ),
                )
            )

            continue

        if tokens and not _mentions(
            text=f"{item.title} {item.url}",
            tokens=tokens,
        ):
            findings.append(
                ValidationFinding(
                    code=ValidationCode.NEWS_OF_ANOTHER_FUND,
                    severity=ValidationSeverity.REJECT,
                    detail=(
                        "The item is published on a shared manager site "
                        "and does not name this fund, so it may belong "
                        f"to another fund of the group: {item.url}"
                    ),
                )
            )

    return findings


def validate_minimum_investment(
    value: MinimumInvestmentValue,
) -> list[ValidationFinding]:
    """Check that an inferred minimum investment states its legal basis."""

    if value.origin is not ValueOrigin.INFERRED:
        return []

    if value.inference is None:
        return [
            ValidationFinding(
                code=ValidationCode.INFERRED_MINIMUM_WITHOUT_BASIS,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The minimum investment was inferred rather than "
                    "read from a source, and no legal basis is recorded."
                ),
            )
        ]

    if not value.inference.legal_basis.strip():
        return [
            ValidationFinding(
                code=ValidationCode.INFERRED_MINIMUM_WITHOUT_BASIS,
                severity=ValidationSeverity.REJECT,
                detail="The recorded legal basis of the inference is empty.",
            )
        ]

    return []


def validate_fee_items(
    items: Sequence[FeeItem],
) -> list[ValidationFinding]:
    """Check that a fee stated as a range still shows both of its bounds."""

    findings: list[ValidationFinding] = []

    for item in items:
        if item.minimum_rate_percent is None and item.maximum_rate_percent is None:
            if _basis_states_a_range(item):
                findings.append(
                    ValidationFinding(
                        code=ValidationCode.COLLAPSED_FEE_RANGE,
                        severity=ValidationSeverity.REVIEW,
                        detail=(
                            f"The {item.type.value} fee is written as a "
                            "range in its source, but only one number is "
                            "stored."
                        ),
                    )
                )

            continue

        if (
            item.rate_percent is not None
            and item.maximum_rate_percent is not None
            and item.minimum_rate_percent is not None
            and item.minimum_rate_percent != item.maximum_rate_percent
            and item.maximum is not True
        ):
            findings.append(
                ValidationFinding(
                    code=ValidationCode.COLLAPSED_FEE_RANGE,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        f"The {item.type.value} fee covers "
                        f"{item.minimum_rate_percent:g} % to "
                        f"{item.maximum_rate_percent:g} %, but its single "
                        "rate is not marked as an upper limit."
                    ),
                )
            )

    return findings


def validate_target_return(
    value: TargetReturnValue,
) -> list[ValidationFinding]:
    """Check that a stated range kept both of its bounds."""

    if (
        value.minimum_percent_pa is not None
        and value.maximum_percent_pa is not None
        and value.value_percent_pa is not None
    ):
        return [
            ValidationFinding(
                code=ValidationCode.COLLAPSED_FEE_RANGE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The return carries both a range and a single value, "
                    "so a reader cannot tell which one the fund states."
                ),
            )
        ]

    return []


def rejects(
    findings: Iterable[ValidationFinding],
) -> bool:
    """Return whether any finding refuses the value outright."""

    return any(finding.severity is ValidationSeverity.REJECT for finding in findings)


def fund_level_capital(
    observations: Iterable[CapitalObservation],
) -> list[CapitalObservation]:
    """Keep only the observations that describe the assets of the fund."""

    return [
        observation
        for observation in observations
        if observation.metric_type not in NON_FUND_CAPITAL_METRICS
    ]


def calendar_year_returns(
    observations: Iterable[AnnualReturnObservation],
) -> list[AnnualReturnObservation]:
    """Keep only the results of a completed calendar year."""

    return [
        observation
        for observation in observations
        if observation.series_type is ReturnSeriesType.CALENDAR_YEAR
    ]


def _duplicate_date_findings(
    *,
    keys: Sequence[object],
    subject: str,
) -> list[ValidationFinding]:
    counted = Counter(keys)

    duplicates = sorted(str(key) for key, count in counted.items() if count > 1)

    if not duplicates:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.DUPLICATE_DATES_IN_SERIES,
            severity=ValidationSeverity.REJECT,
            detail=(
                f"The {subject} reports the same date more than once: " + ", ".join(duplicates[:5])
            ),
        )
    ]


def _basis_states_a_range(
    item: FeeItem,
) -> bool:
    """Return whether the quoted source text describes a range."""

    text = normalize_search_text(f"{item.basis or ''} {item.condition or ''} {item.details or ''}")

    if not text:
        return False

    return (
        any(
            marker in text
            for marker in (
                " az ",
                " do ",
                "od 0",
                "range",
                " to ",
            )
        )
        and text.count("%") >= 2
    )


def _distinctive_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    """Return the words that tell this fund apart from its siblings."""

    generic = {
        "a",
        "as",
        "s",
        "sicav",
        "fond",
        "fondy",
        "fund",
        "funds",
        "investicni",
        "investment",
        "spolecnost",
        "podfond",
        "subfund",
        "otevreny",
        "uzavreny",
        "promennym",
        "zakladnim",
        "kapitalem",
    }

    normalized = normalize_search_text(fund_name)

    return tuple(
        token
        for token in normalized.replace(
            ",",
            " ",
        ).split()
        if len(token) > 2 and token.strip(".") not in generic
    )


def _mentions(
    *,
    text: str,
    tokens: Sequence[str],
) -> bool:
    normalized = normalize_search_text(
        text.replace(
            "-",
            " ",
        )
    )

    return all(token.strip(".") in normalized for token in tokens)
