"""
Validation rules of the fund data set.

Every rule here names one way in which a value can be wrong while still
being well formed. A series of two different metrics validates against
the schema, a statutory minimum capital is a perfectly good amount, and
a news item of a neighbouring fund is a perfectly good article. Only a
rule that knows what the value is supposed to mean can reject them.

The rules are pure: they read a value and return findings. The extraction
uses them to refuse a defective value, and the audit uses the same rules
to explain a delivered one, so both agree on what "wrong" means. There is
one vocabulary of reason codes, ``ValidationCode``, and one severity
scale, and every consumer reports in them.

Step 4 widened the module from the extended fields to every delivered
field, and added the specification each field is checked against, the
traceability rules a found value must satisfy, and the cross-field rules
that only make sense once a whole fund record is in hand.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from fundscraper.field_definitions import (
    HORIZON_MAXIMUM_MARKERS,
    NON_FUND_CAPITAL_METRICS,
    classify_capital_metric,
    classify_horizon_kind,
    fold_diacritics,
    states_a_horizon_range,
    states_benchmark_linked_return,
)
from fundscraper.html_discovery import normalize_search_text
from fundscraper.normalization import canonical_domain
from fundscraper.output_models import (
    Annualization,
    AnnualReturnObservation,
    AssetsUnderManagementValue,
    AumMetricType,
    CapitalObservation,
    Confidence,
    FeeItem,
    FundNewsItem,
    FundParty,
    HistoricalValueSeries,
    HistoricalValueType,
    HorizonKind,
    InvestmentHorizonValue,
    MinimumInvestmentValue,
    NewsSourceType,
    PartyRole,
    ReturnSeriesType,
    ReturnType,
    ScopeType,
    TargetReturnValue,
    ValueOrigin,
)

# The version of the rules below, and of the evidence rules in
# ``output_audit`` that read them. It is the only thing that tells a
# reader whether an audit report describes the current understanding of
# the data, because the file being audited can stay byte-identical while
# what counts as a defect changes underneath it: one report of
# ``funds.after-fast.json`` withheld 138 fields and the next report of
# the same bytes withheld 194.
#
# Bump this whenever a rule is added, removed or changed in a way that
# would alter what an audit reports. Delivery refuses any audit stamped
# with a different value, so a stale report cannot hand a doubted value
# over as clean.
AUDIT_RULESET_VERSION: Final = "2026-08-12.3"


# The shape of the audit report itself. Independent of the rules: a
# reader parsing the report cares about this, a reader trusting its
# verdicts cares about the one above.
AUDIT_SCHEMA_VERSION: Final = "2"


class ValidationCode(StrEnum):
    """
    Why one value cannot be trusted as it stands.

    The string of every member is the reason code written into the audit
    report. The members added after a code was already delivered keep the
    string it was delivered under, so a report of an earlier run and a
    report of this one can be compared line by line.
    """

    # Provenance. A value that cannot be traced is kept, but a reader
    # has no way of checking it.
    MISSING_SOURCE = "missing_source"
    MISSING_SOURCE_DATE = "missing_source_date"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_PAGE_REFERENCE = "missing_page_reference"
    EVIDENCE_DOES_NOT_SUPPORT_VALUE = "evidence_does_not_support_value"
    SCOPE_MISMATCH = "scope_mismatch"
    THIRD_PARTY_SOURCE = "third_party_source"
    SOURCE_SHARED_ACROSS_FUNDS = "source_shared_across_funds"
    SOURCE_DOES_NOT_NAME_THE_FUND = "source_does_not_name_the_fund"

    # Shape. The value is present but incomplete.
    MISSING_VALUE_COMPONENT = "missing_value_component"
    MISSING_CURRENCY = "missing_currency"
    MISSING_AS_OF_DATE = "missing_as_of_date"
    MALFORMED_VALUE = "malformed_value"
    UNSUPPORTED_CURRENCY = "unsupported_currency"

    # Investment horizon.
    IMPLAUSIBLE_HORIZON = "implausible_horizon"
    CALENDAR_YEAR_AS_HORIZON = "calendar_year_as_horizon"
    HORIZON_BOUND_LOST = "horizon_bound_lost"

    # Minimum investment.
    ZERO_MINIMUM_INVESTMENT = "zero_minimum_investment"
    IMPLAUSIBLY_SMALL_MINIMUM_INVESTMENT = "implausibly_small_minimum_investment"
    BELOW_QUALIFIED_INVESTOR_THRESHOLD = "below_qualified_investor_threshold"
    NON_ROUND_MINIMUM_INVESTMENT = "non_round_minimum_investment"
    INFERRED_MINIMUM_WITHOUT_BASIS = "inferred_minimum_without_basis"
    STATUTORY_CAPITAL_AS_MINIMUM_INVESTMENT = "statutory_capital_as_minimum_investment"
    PERCENTAGE_AS_MINIMUM_INVESTMENT = "percentage_as_minimum_investment"

    # Target return.
    ZERO_TARGET_RETURN = "zero_target_return"
    IMPLAUSIBLE_TARGET_RETURN = "implausible_target_return"
    UNUSUALLY_HIGH_TARGET_RETURN = "unusually_high_target_return"
    YEAR_CAPTURED_AS_PERCENTAGE = "year_captured_as_percentage"
    INVERTED_TARGET_RETURN_RANGE = "inverted_target_return_range"
    COLLAPSED_TARGET_RETURN_RANGE = "collapsed_target_return_range"
    KID_SCENARIO_AS_TARGET_RETURN = "kid_performance_scenario_as_target"
    UNRELATED_PERCENTAGE_AS_TARGET_RETURN = "unrelated_percentage_as_target_return"
    HISTORICAL_RETURN_AS_TARGET_RETURN = "historical_return_as_target_return"
    UNKNOWN_ANNUALIZATION_FOR_ANNUAL_RATE = "unknown_annualization_for_annual_rate"
    BENCHMARK_RETURN_AS_FIXED_RATE = "benchmark_return_as_fixed_rate"

    # Fees.
    COLLAPSED_FEE_RANGE = "collapsed_fee_range"
    IMPLAUSIBLE_FEE_RATE = "implausible_fee_rate"
    UNUSUALLY_HIGH_FEE_RATE = "unusually_high_fee_rate"
    FEE_AMOUNT_RATE_MISMATCH = "fee_amount_rate_mismatch"
    ZERO_FEE_WITHOUT_EVIDENCE = "zero_fee_without_evidence"
    ZERO_FEE_FROM_A_RANGE = "zero_fee_from_a_range"
    ZERO_FEE_NOT_SUPPORTED_BY_SOURCE = "zero_fee_not_supported_by_source"
    ZERO_FEE_OF_ANOTHER_FEE_TYPE = "zero_fee_of_another_fee_type"
    VALUE_NOT_PRESENT_IN_EVIDENCE = "value_not_present_in_evidence"
    VALUE_FAR_FROM_FEE_LABEL = "value_far_from_fee_label"
    PERCENTAGE_DESCRIBES_INCOME_SHARE = "percentage_describes_income_share"
    MAXIMUM_FLAG_CONTRADICTS_SOURCE = "maximum_flag_contradicts_source"

    # Assets under management and capital series.
    STATUTORY_CAPITAL_AS_AUM = "statutory_capital_as_aum"
    MANAGER_AUM_AS_FUND_AUM = "manager_aum_as_fund_aum"
    PER_SHARE_VALUE_AS_AUM = "per_share_value_as_aum"
    IMPLAUSIBLY_SMALL_AUM = "implausibly_small_aum"
    IMPLAUSIBLY_LARGE_AUM = "implausibly_large_aum"
    IMPLAUSIBLE_AUM_AMOUNT = "implausible_aum_amount"
    THOUSANDS_UNIT_NOT_APPLIED = "thousands_unit_not_applied"
    AUM_VALUE_NOT_TIED_TO_LABEL = "aum_value_not_tied_to_label"
    CAPITAL_OF_ANOTHER_COMPANY = "capital_of_another_company"
    CAPITAL_METRIC_CONTRADICTS_EVIDENCE = "capital_metric_contradicts_evidence"
    NEWER_AUM_OBSERVATION_EXISTS = "newer_aum_observation_exists"
    FUTURE_AS_OF_DATE = "future_as_of_date"
    STALE_AS_OF_DATE = "stale_as_of_date"

    # Series consistency.
    MIXED_METRICS_IN_SERIES = "mixed_metrics_in_series"
    MIXED_SHARE_CLASSES_IN_SERIES = "mixed_share_classes_in_series"
    DUPLICATE_DATES_IN_SERIES = "duplicate_dates_in_series"
    MIXED_CURRENCIES_IN_SERIES = "mixed_currencies_in_series"

    # Annual returns.
    CONFLICTING_ANNUAL_RETURNS = "conflicting_annual_returns"
    CUMULATIVE_AS_ANNUAL_RETURN = "cumulative_as_annual_return"
    KID_SCENARIO_AS_ANNUAL_RETURN = "kid_scenario_as_annual_return"

    # Parties.
    PARTY_NAME_NOT_SPECIFIC = "party_name_not_specific"
    PARTY_ROLE_MISMATCH = "party_role_mismatch"
    PARTY_IS_THE_FUND_ITSELF = "party_is_the_fund_itself"
    PARTY_NAME_CONTAINS_A_DATE = "party_name_contains_a_date"
    MALFORMED_REGISTRATION_NUMBER = "malformed_registration_number"

    # News.
    NEWS_OF_ANOTHER_FUND = "news_of_another_fund"
    NEWS_WITHOUT_DATE = "news_without_date"
    NEWS_FROM_UNOFFICIAL_SOURCE = "news_from_unofficial_source"

    # Cross-field.
    CONFLICTING_VALUES = "conflicting_values"

    # Fallback parser.
    FALLBACK_PARSER_REVIEW_REQUIRED = "fallback_parser_review_required"


class ValidationSeverity(StrEnum):
    """
    What a finding does to the value it describes.

    ``REVIEW`` keeps the value and asks a human to look at it. ``CONFLICT``
    and ``REJECT`` both refuse it, and the difference is why: a conflict
    means two sources say different things and neither can be preferred,
    a rejection means the value means something other than the field it
    was written into.
    """

    REVIEW = "review"
    CONFLICT = "conflict"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: ValidationCode
    severity: ValidationSeverity
    detail: str

    # Which delivered field a reviewer has to open. A rule that checks
    # one field leaves it empty, because the field is already known; a
    # cross-field rule names the weaker of the two it compared.
    subject: str | None = None


# ---------------------------------------------------------------------------
# Calibrated thresholds
#
# Every number below was chosen after reading the distribution of the
# 341-fund output in data/output/funds.full.json, not from a guess about
# what a fund ought to look like.
# ---------------------------------------------------------------------------


# The delivered horizons run from one to ten years. Thirty years is well
# beyond the longest closed-end Czech qualified investor fund and still
# leaves every real value untouched.
HORIZON_MAXIMUM_YEARS: Final = 30.0

YEAR_LIKE_MINIMUM: Final = 1_900.0
YEAR_LIKE_MAXIMUM: Final = 2_100.0


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


# The statutory minimum fund capital of a Czech qualified investor fund.
# Read as a subscription minimum it turns the floor the law puts under
# the fund into the amount an investor has to bring.
STATUTORY_FUND_CAPITAL: Final[dict[str, tuple[float, ...]]] = {
    "EUR": (1_250_000.0,),
    "CZK": (32_000_000.0,),
}


STATUTORY_CAPITAL_MARKERS: Final[tuple[str, ...]] = (
    "zakladni kapital",
    "minimalni vyse fondoveho kapitalu",
    "nejnizsi pripustna vyse fondoveho kapitalu",
    "zapisovany zakladni kapital",
)


# Delivered target returns cluster between 4 and 15 per cent a year.
# Above 25 the figure is usually a cumulative result, a model calculation
# or a share of something that is not a return at all.
TARGET_RETURN_HIGH: Final = 25.0
TARGET_RETURN_IMPLAUSIBLE: Final = 100.0
TARGET_RETURN_KID_SUSPICIOUS: Final = 15.0


# Wording proving a percentage measures something other than a return.
# Kept deliberately narrow: every marker was read in a real quote of the
# delivered output before it was added, and generic ones such as "LTV"
# were dropped because they appear next to genuine targets.
UNRELATED_PERCENTAGE_MARKERS: Final[tuple[str, ...]] = (
    "obsazenost",
    "obsazenosti",
    "zaplnenost",
    "pronajato",
    "rozestaven",
    "dokoncenost",
    "dokonceno",
    "stavebni pripravenost",
    "postaveno",
    "podil na hlasovacich pravech",
    "vlastnicky podil",
    "podilem ve spolecnosti",
    "of its own capital",
    "modeloveho zisku",
    "modelovy zisk",
    "occupancy",
    "completion rate",
)


# Wording of a key information document that reports a projection or a
# cost impact rather than what the fund aims for.
KID_SCENARIO_MARKERS: Final[tuple[str, ...]] = (
    "vnitrni vynosnost",
    "irr",
    "scenar",
    "stresovy",
    "nepriznivy",
    "priznivy",
    "umerny",
    "performance scenario",
    "moderate scenario",
    "unfavourable",
)


# Wording of an achieved result. A past return is only a target when the
# fund says so, and none of these say so.
HISTORICAL_RETURN_MARKERS: Final[tuple[str, ...]] = (
    "zhodnoceni v roce",
    "vykonnost v roce",
    "vykonnost fondu v roce",
    "historicka vykonnost",
    "dosazene zhodnoceni",
    "zhodnoceni za rok",
    "past performance",
    "historical performance",
)


# Manual review of the delivered data showed that high exit and
# performance fees are genuine in Czech qualified investor funds, for
# example a 95 per cent exit fee during the investment period or a 45 per
# cent performance fee above a hurdle. Only the fee types that are
# capped in practice are checked against a magnitude threshold.
FEE_RATE_IMPLAUSIBLE: Final = 100.0

FEE_RATE_HIGH_BY_TYPE: Final[dict[str, float]] = {
    "entry": 10.0,
    "management": 10.0,
    "administration": 10.0,
    "depositary": 10.0,
    "ongoing": 15.0,
    "transaction": 15.0,
}


# A fund's capital is never a handful of crowns. A capital figure below
# this bound is a per-share value: the delivered output holds net-asset
# series of 0.09 CZK and NAV series of 0.86 CZK, which are unit prices
# read into a fund-level series.
PER_SHARE_VALUE_LIMIT: Final = 1_000.0


# A fund of any size holds far more than this. Smaller reported assets
# almost always mean a thousands unit of a Czech annual report was not
# applied, because those statements are published "v tis. Kc".
AUM_IMPLAUSIBLE_AMOUNT: Final = 1_000_000.0


# The whole Czech qualified investor sector is worth a few hundred
# billion crowns, and the largest single fund in the delivered output
# holds six billion. Half a trillion in one fund is a multiplier applied
# twice, as in the 4.4 trillion CZK read for one industrial fund.
AUM_IMPLAUSIBLY_LARGE_AMOUNT: Final = 500_000_000_000.0


AUM_MAXIMUM_AGE_DAYS: Final = 5 * 365


# A calendar-year return of a qualified investor fund lies well inside
# this band. A larger figure is a cumulative result of several years
# presented in a yearly table.
PLAUSIBLE_ANNUAL_RETURN_LIMIT: Final = 60.0


# Two reports of the same year may round differently. A larger gap means
# the two figures describe different things.
ANNUAL_RETURN_TOLERANCE: Final = 0.05


# Two capital figures of the same date may be rounded differently by the
# report that carries them. A wider gap means they measure two things.
AUM_AGREEMENT_TOLERANCE: Final = 0.01


# The currencies the delivered fields may use. Everything else is a
# parsing artefact of a symbol table rather than a real denomination.
ACCEPTED_CURRENCIES: Final[frozenset[str]] = frozenset({"CZK", "EUR", "USD"})


# ---------------------------------------------------------------------------
# Field specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpecification:
    """
    What one delivered field is allowed to contain.

    The specification is data rather than code so that the audit can
    publish it next to its findings: a reader of the report sees the rule
    a value was measured against, not only that it failed.
    """

    field: str
    value_kind: str
    unit: str | None = None
    currencies: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    requires_as_of_date: bool = False
    requires_source: bool = True
    requires_evidence: bool = True
    requires_page_in_paginated_sources: bool = True
    accepted_scopes: tuple[ScopeType, ...] = (
        ScopeType.FUND,
        ScopeType.SUBFUND,
        ScopeType.SHARE_CLASS,
    )
    hard_reject: tuple[ValidationCode, ...] = ()
    review_only: tuple[ValidationCode, ...] = ()
    notes: str = ""


FIELD_SPECIFICATIONS: Final[dict[str, FieldSpecification]] = {
    "investment_horizon": FieldSpecification(
        field="investment_horizon",
        value_kind="duration",
        unit="years",
        minimum=0.0,
        maximum=HORIZON_MAXIMUM_YEARS,
        hard_reject=(ValidationCode.CALENDAR_YEAR_AS_HORIZON,),
        review_only=(
            ValidationCode.IMPLAUSIBLE_HORIZON,
            ValidationCode.HORIZON_BOUND_LOST,
        ),
        notes=(
            "A duration in years, read from a recommended holding period. "
            "A calendar year or a date is never a horizon."
        ),
    ),
    "minimum_investment": FieldSpecification(
        field="minimum_investment",
        value_kind="money",
        unit="currency amount",
        currencies=tuple(sorted(ACCEPTED_CURRENCIES)),
        minimum=0.0,
        hard_reject=(
            ValidationCode.INFERRED_MINIMUM_WITHOUT_BASIS,
            ValidationCode.STATUTORY_CAPITAL_AS_MINIMUM_INVESTMENT,
            ValidationCode.PERCENTAGE_AS_MINIMUM_INVESTMENT,
        ),
        review_only=(
            ValidationCode.ZERO_MINIMUM_INVESTMENT,
            ValidationCode.IMPLAUSIBLY_SMALL_MINIMUM_INVESTMENT,
            ValidationCode.BELOW_QUALIFIED_INVESTOR_THRESHOLD,
            ValidationCode.NON_ROUND_MINIMUM_INVESTMENT,
        ),
        notes=(
            "A subscription amount in a currency. Never a percentage, a "
            "share price, a unit value or the statutory fund capital. An "
            "inferred value must carry the provision it rests on."
        ),
    ),
    "target_return": FieldSpecification(
        field="target_return",
        value_kind="rate",
        unit="per cent a year",
        minimum=0.0,
        maximum=TARGET_RETURN_IMPLAUSIBLE,
        hard_reject=(
            ValidationCode.KID_SCENARIO_AS_TARGET_RETURN,
            ValidationCode.UNRELATED_PERCENTAGE_AS_TARGET_RETURN,
            ValidationCode.HISTORICAL_RETURN_AS_TARGET_RETURN,
            ValidationCode.YEAR_CAPTURED_AS_PERCENTAGE,
        ),
        review_only=(
            ValidationCode.UNUSUALLY_HIGH_TARGET_RETURN,
            ValidationCode.UNKNOWN_ANNUALIZATION_FOR_ANNUAL_RATE,
            ValidationCode.BENCHMARK_RETURN_AS_FIXED_RATE,
            ValidationCode.IMPLAUSIBLE_TARGET_RETURN,
            ValidationCode.ZERO_TARGET_RETURN,
            ValidationCode.COLLAPSED_TARGET_RETURN_RANGE,
        ),
        notes=(
            "A return concept the fund states about the future: expected, "
            "target, preferred, hurdle, guaranteed minimum, a range, or "
            "explicitly not published. The distinction between them is "
            "kept. An achieved result, a KID scenario, an occupancy or an "
            "ownership share is not a target return."
        ),
    ),
    "fees": FieldSpecification(
        field="fees",
        value_kind="collection",
        unit="per cent or currency amount",
        currencies=tuple(sorted(ACCEPTED_CURRENCIES)),
        minimum=0.0,
        review_only=(
            ValidationCode.COLLAPSED_FEE_RANGE,
            ValidationCode.IMPLAUSIBLE_FEE_RATE,
            ValidationCode.UNUSUALLY_HIGH_FEE_RATE,
            ValidationCode.FEE_AMOUNT_RATE_MISMATCH,
            ValidationCode.ZERO_FEE_OF_ANOTHER_FEE_TYPE,
            ValidationCode.MISSING_CURRENCY,
        ),
        notes=(
            "One item per fee type, each either a rate or a fixed amount "
            "with its currency. Tiers and ranges are preserved; a table "
            "row collapsed onto several fee types is refused."
        ),
    ),
    "assets_under_management": FieldSpecification(
        field="assets_under_management",
        value_kind="money",
        unit="currency amount",
        currencies=tuple(sorted(ACCEPTED_CURRENCIES)),
        minimum=AUM_IMPLAUSIBLE_AMOUNT,
        maximum=AUM_IMPLAUSIBLY_LARGE_AMOUNT,
        requires_as_of_date=True,
        hard_reject=(
            ValidationCode.MANAGER_AUM_AS_FUND_AUM,
            ValidationCode.STATUTORY_CAPITAL_AS_AUM,
            ValidationCode.CAPITAL_OF_ANOTHER_COMPANY,
        ),
        review_only=(
            ValidationCode.CAPITAL_METRIC_CONTRADICTS_EVIDENCE,
            ValidationCode.NEWER_AUM_OBSERVATION_EXISTS,
            ValidationCode.PER_SHARE_VALUE_AS_AUM,
            ValidationCode.IMPLAUSIBLY_SMALL_AUM,
            ValidationCode.IMPLAUSIBLY_LARGE_AUM,
            ValidationCode.THOUSANDS_UNIT_NOT_APPLIED,
            ValidationCode.STALE_AS_OF_DATE,
            ValidationCode.FUTURE_AS_OF_DATE,
        ),
        notes=(
            "The assets of this fund or subfund on a stated date. Never "
            "the assets of the manager, the registered or statutory "
            "capital, or a value per investment share."
        ),
    ),
    "manager": FieldSpecification(
        field="manager",
        value_kind="party",
        requires_page_in_paginated_sources=True,
        accepted_scopes=(
            ScopeType.FUND,
            ScopeType.SUBFUND,
            ScopeType.SHARE_CLASS,
            ScopeType.MANAGER,
        ),
        hard_reject=(ValidationCode.PARTY_ROLE_MISMATCH,),
        review_only=(
            ValidationCode.PARTY_NAME_NOT_SPECIFIC,
            ValidationCode.PARTY_IS_THE_FUND_ITSELF,
            ValidationCode.PARTY_NAME_CONTAINS_A_DATE,
            ValidationCode.MALFORMED_REGISTRATION_NUMBER,
        ),
        notes=(
            "The company that manages the fund (obhospodarovatel). A "
            "legal form on its own, a sentence fragment or the fund "
            "itself is not a manager."
        ),
    ),
    "administrator": FieldSpecification(
        field="administrator",
        value_kind="party",
        accepted_scopes=(
            ScopeType.FUND,
            ScopeType.SUBFUND,
            ScopeType.SHARE_CLASS,
            ScopeType.MANAGER,
        ),
        hard_reject=(ValidationCode.PARTY_ROLE_MISMATCH,),
        review_only=(
            ValidationCode.PARTY_NAME_NOT_SPECIFIC,
            ValidationCode.PARTY_IS_THE_FUND_ITSELF,
            ValidationCode.PARTY_NAME_CONTAINS_A_DATE,
            ValidationCode.MALFORMED_REGISTRATION_NUMBER,
        ),
        notes=(
            "The company that administers the fund (administrator). It is "
            "often the same company as the manager, which is normal and "
            "not a conflict, but the roles are never swapped."
        ),
    ),
    "aum_history": FieldSpecification(
        field="aum_history",
        value_kind="series",
        unit="currency amount",
        currencies=tuple(sorted(ACCEPTED_CURRENCIES)),
        requires_as_of_date=True,
        hard_reject=(
            ValidationCode.STATUTORY_CAPITAL_AS_AUM,
            ValidationCode.MANAGER_AUM_AS_FUND_AUM,
            ValidationCode.DUPLICATE_DATES_IN_SERIES,
            ValidationCode.CAPITAL_OF_ANOTHER_COMPANY,
        ),
        review_only=(
            ValidationCode.PER_SHARE_VALUE_AS_AUM,
            ValidationCode.IMPLAUSIBLE_AUM_AMOUNT,
            ValidationCode.IMPLAUSIBLY_LARGE_AUM,
            ValidationCode.IMPLAUSIBLY_SMALL_AUM,
            ValidationCode.THOUSANDS_UNIT_NOT_APPLIED,
            ValidationCode.MIXED_METRICS_IN_SERIES,
            ValidationCode.FUTURE_AS_OF_DATE,
        ),
        notes=(
            "Dated fund-level capital figures. Every observation carries "
            "its metric, so a registered capital and a net asset value "
            "are never added to the same line."
        ),
    ),
    "annual_returns": FieldSpecification(
        field="annual_returns",
        value_kind="series",
        unit="per cent",
        requires_as_of_date=True,
        hard_reject=(
            ValidationCode.CUMULATIVE_AS_ANNUAL_RETURN,
            ValidationCode.KID_SCENARIO_AS_ANNUAL_RETURN,
            ValidationCode.DUPLICATE_DATES_IN_SERIES,
        ),
        review_only=(
            ValidationCode.MIXED_METRICS_IN_SERIES,
            ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES,
        ),
        notes=(
            "One observation per completed calendar year, each naming its "
            "year and its share class. Year-to-date, rolling, cumulative "
            "and annualized figures keep their own series type."
        ),
    ),
    "historical_values": FieldSpecification(
        field="historical_values",
        value_kind="series",
        currencies=tuple(sorted(ACCEPTED_CURRENCIES)),
        requires_as_of_date=True,
        hard_reject=(ValidationCode.DUPLICATE_DATES_IN_SERIES,),
        review_only=(
            ValidationCode.MIXED_METRICS_IN_SERIES,
            ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES,
            ValidationCode.MIXED_CURRENCIES_IN_SERIES,
            ValidationCode.PER_SHARE_VALUE_AS_AUM,
            ValidationCode.IMPLAUSIBLY_SMALL_AUM,
            ValidationCode.THOUSANDS_UNIT_NOT_APPLIED,
            ValidationCode.FUTURE_AS_OF_DATE,
        ),
        notes=(
            "One series per measured quantity, share class and currency. "
            "A NAV per share, a fund capital and the assets under "
            "management never share a series."
        ),
    ),
    "news": FieldSpecification(
        field="news",
        value_kind="collection",
        requires_page_in_paginated_sources=False,
        accepted_scopes=(ScopeType.FUND, ScopeType.SUBFUND),
        hard_reject=(ValidationCode.NEWS_OF_ANOTHER_FUND,),
        review_only=(
            ValidationCode.NEWS_WITHOUT_DATE,
            ValidationCode.NEWS_FROM_UNOFFICIAL_SOURCE,
        ),
        notes=(
            "Articles published by the fund or its manager about this "
            "fund. Corporate news of the manager that never names the "
            "fund is not fund news."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueProvenance:
    """What is known about where one delivered value came from."""

    field: str
    source_url: str | None = None
    retrieved_at: str | None = None
    quote: str | None = None
    page: int | None = None
    scope_type: str | None = None
    confidence: str | None = None
    review_required: bool | None = None

    # The numbers a reader has to be able to find in the quote. An empty
    # tuple means the value is not numeric and the check does not apply.
    supporting_numbers: tuple[float, ...] = ()

    # Whether the file this value comes from stores evidence at all. The
    # reduced delivery schema has no place for a quote or a page, and
    # reporting every one of its fields as unevidenced would say something
    # about the schema rather than about the data.
    evidence_available: bool = True


def validate_provenance(
    provenance: ValueProvenance,
) -> list[ValidationFinding]:
    """
    Check that a found value can be traced back to what it was read from.

    None of these findings refuses a value. An untraceable value may
    still be the right one, and deleting it would lose information a
    reviewer needs; what it loses is the right to be trusted unchecked.
    """

    specification = FIELD_SPECIFICATIONS.get(provenance.field)

    findings: list[ValidationFinding] = []

    if not provenance.source_url:
        return [
            ValidationFinding(
                code=ValidationCode.MISSING_SOURCE,
                severity=ValidationSeverity.REVIEW,
                detail="A found value carries no source reference at all.",
            )
        ]

    if not provenance.retrieved_at:
        findings.append(
            ValidationFinding(
                code=ValidationCode.MISSING_SOURCE_DATE,
                severity=ValidationSeverity.REVIEW,
                detail=("The source has no retrieval date, so the value cannot be aged."),
            )
        )

    if not provenance.evidence_available:
        return findings + _scope_findings(
            provenance=provenance,
            specification=specification,
        )

    if not (provenance.quote or "").strip():
        findings.append(
            ValidationFinding(
                code=ValidationCode.MISSING_EVIDENCE,
                severity=ValidationSeverity.REVIEW,
                detail=("The value keeps no quoted source text, so nothing supports it."),
            )
        )
    elif provenance.supporting_numbers and not any(
        quote_supports_number(
            quote=provenance.quote or "",
            value=number,
        )
        for number in provenance.supporting_numbers
    ):
        findings.append(
            ValidationFinding(
                code=ValidationCode.EVIDENCE_DOES_NOT_SUPPORT_VALUE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "None of the numbers of the value appears in the quoted "
                    "source text, in any unit it could have been written in."
                ),
            )
        )

    # A page number is what lets a reader open the document and see the
    # sentence. Inventing one for a web page would be worse than having
    # none, so only a source that really has pages is required to name one.
    if (
        specification is not None
        and specification.requires_page_in_paginated_sources
        and provenance.page is None
        and is_paginated_source(provenance.source_url)
    ):
        findings.append(
            ValidationFinding(
                code=ValidationCode.MISSING_PAGE_REFERENCE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The source is a paginated document and the value names "
                    "no page, so the quote cannot be located in it."
                ),
            )
        )

    return findings + _scope_findings(
        provenance=provenance,
        specification=specification,
    )


def _scope_findings(
    *,
    provenance: ValueProvenance,
    specification: FieldSpecification | None,
) -> list[ValidationFinding]:
    if specification is None or not provenance.scope_type:
        return []

    accepted = {scope.value for scope in specification.accepted_scopes}

    if provenance.scope_type in accepted:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.SCOPE_MISMATCH,
            severity=ValidationSeverity.REVIEW,
            detail=(
                f"The value is attributed to a {provenance.scope_type} "
                f"scope, which {provenance.field} does not accept."
            ),
        )
    ]


def is_paginated_source(
    url: str,
) -> bool:
    """Return whether a source is a document that has page numbers."""

    path = urlsplit(url).path.casefold()

    return path.endswith(".pdf")


def quote_supports_number(
    *,
    quote: str,
    value: float,
) -> bool:
    """
    Return whether a quoted source text really contains one number.

    A quote states "179 564 tis. Kc" for a value of 179 564 000, and
    "3,645 mld. Kc" for 3 645 000 000. Comparing the digits alone would
    call both unsupported, so every unit a Czech document writes amounts
    in is tried, and a rounded presentation is accepted within half a
    per cent of the value.
    """

    if value == 0:
        return "0" in quote

    present = _numbers_in(quote)

    for scale in (1.0, 1e3, 1e6, 1e9, 1e-2, 1e2):
        target = value / scale

        for candidate in present:
            if abs(candidate - target) <= abs(target) * 0.005:
                return True

    return False


# The spaces a Czech document groups thousands with: an ordinary space,
# a non-breaking one, a narrow one, a thin one and a figure space.
_DIGIT_GROUP_SEPARATORS: Final = "     "

_GROUPED_NUMBER_PATTERN: Final = re.compile(
    r"(?<=\d)[" + _DIGIT_GROUP_SEPARATORS + r"](?=\d)",
)

_NUMBER_PATTERN: Final = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers_in(
    text: str,
) -> list[float]:
    joined = _GROUPED_NUMBER_PATTERN.sub(
        "",
        text,
    )

    values: list[float] = []

    for match in _NUMBER_PATTERN.finditer(joined):
        try:
            values.append(float(match.group().replace(",", ".")))
        except ValueError:
            continue

    return values


# ---------------------------------------------------------------------------
# Base fields
# ---------------------------------------------------------------------------


def validate_investment_horizon(
    value: InvestmentHorizonValue,
) -> list[ValidationFinding]:
    """Check that a recommended horizon is a duration and a realistic one."""

    years = value.recommended_years

    if YEAR_LIKE_MINIMUM <= years <= YEAR_LIKE_MAXIMUM:
        return [
            ValidationFinding(
                code=ValidationCode.CALENDAR_YEAR_AS_HORIZON,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"A horizon of {years:g} lies in the range of a calendar "
                    "year, so a year was captured instead of a duration."
                ),
            )
        ]

    if years <= 0 or years > HORIZON_MAXIMUM_YEARS:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLE_HORIZON,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"A recommended horizon of {years:g} years is outside the "
                    f"plausible range of 0 to {HORIZON_MAXIMUM_YEARS:g} years."
                ),
            )
        ]

    return []


def validate_minimum_investment(
    value: MinimumInvestmentValue,
    *,
    quote: str | None = None,
) -> list[ValidationFinding]:
    """Check that a minimum investment is a real subscription amount."""

    findings: list[ValidationFinding] = []

    findings.extend(_inference_findings(value))

    if value.currency not in ACCEPTED_CURRENCIES:
        findings.append(
            ValidationFinding(
                code=ValidationCode.UNSUPPORTED_CURRENCY,
                severity=ValidationSeverity.REVIEW,
                detail=(f"{value.currency} is not a currency this data set reports amounts in."),
            )
        )

    normalized_quote = normalize_search_text(quote or "")

    amount = value.amount

    if amount in STATUTORY_FUND_CAPITAL.get(value.currency, ()) and any(
        marker in normalized_quote for marker in STATUTORY_CAPITAL_MARKERS
    ):
        findings.append(
            ValidationFinding(
                code=(ValidationCode.STATUTORY_CAPITAL_AS_MINIMUM_INVESTMENT),
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"{amount:,.0f} {value.currency} is the statutory fund "
                    "capital named in the quoted text, not what an investor "
                    "has to subscribe."
                ),
            )
        )

    if amount == 0:
        findings.append(
            ValidationFinding(
                code=ValidationCode.ZERO_MINIMUM_INVESTMENT,
                severity=ValidationSeverity.REVIEW,
                detail=("A minimum investment of zero cannot be a real subscription limit."),
            )
        )
    elif amount < MINIMUM_INVESTMENT_IMPLAUSIBLE.get(
        value.currency,
        0.0,
    ):
        findings.append(
            ValidationFinding(
                code=(ValidationCode.IMPLAUSIBLY_SMALL_MINIMUM_INVESTMENT),
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{amount:,.2f} {value.currency} is far below any realistic "
                    "subscription and usually indicates a nominal share price, "
                    "a unit value or a lost thousands multiplier."
                ),
            )
        )
    elif amount < MINIMUM_INVESTMENT_BELOW_THRESHOLD.get(
        value.currency,
        0.0,
    ):
        findings.append(
            ValidationFinding(
                code=(ValidationCode.BELOW_QUALIFIED_INVESTOR_THRESHOLD),
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{amount:,.2f} {value.currency} is well below the "
                    "qualified investor entry threshold, so it may describe a "
                    "savings plan instalment or a different fee."
                ),
            )
        )

    if amount and amount != round(amount):
        findings.append(
            ValidationFinding(
                code=ValidationCode.NON_ROUND_MINIMUM_INVESTMENT,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{amount:,.2f} {value.currency} is not a round "
                    "subscription amount and looks like a unit price or an "
                    "exchange rate."
                ),
            )
        )

    return findings


def _inference_findings(
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


def validate_target_return(
    value: TargetReturnValue,
    *,
    quote: str | None = None,
    source_url: str | None = None,
) -> list[ValidationFinding]:
    """
    Check that a stated percentage really is a forward-looking return.

    The wording around the number decides more than its size does. A
    memorandum reporting a model profit of 41.84 per cent, a web page
    writing "30 % of its own capital" under a target-return heading and
    a key information document stating an internal rate of return are all
    well-formed percentages that mean something else.
    """

    findings: list[ValidationFinding] = []

    candidates = [
        item
        for item in (
            value.value_percent_pa,
            value.minimum_percent_pa,
            value.maximum_percent_pa,
        )
        if item is not None
    ]

    if not candidates:
        return [
            ValidationFinding(
                code=ValidationCode.MISSING_VALUE_COMPONENT,
                severity=ValidationSeverity.REVIEW,
                detail=("The target return contains neither an exact value nor a range."),
            )
        ]

    if (
        value.minimum_percent_pa is not None
        and value.maximum_percent_pa is not None
        and value.value_percent_pa is not None
    ):
        findings.append(
            ValidationFinding(
                code=ValidationCode.COLLAPSED_TARGET_RETURN_RANGE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The return carries both a range and a single value, "
                    "so a reader cannot tell which one the fund states."
                ),
            )
        )

    if (
        value.minimum_percent_pa is not None
        and value.maximum_percent_pa is not None
        and value.minimum_percent_pa > value.maximum_percent_pa
    ):
        findings.append(
            ValidationFinding(
                code=ValidationCode.INVERTED_TARGET_RETURN_RANGE,
                severity=ValidationSeverity.CONFLICT,
                detail=(
                    f"The range minimum {value.minimum_percent_pa:g} % exceeds "
                    f"the maximum {value.maximum_percent_pa:g} %."
                ),
            )
        )

    normalized_quote = normalize_search_text(quote or "")

    findings.extend(
        _target_return_wording_findings(
            normalized_quote=normalized_quote,
            source_url=source_url,
            highest=max(candidates),
            return_type=value.return_type,
        )
    )

    findings.extend(_target_return_magnitude_findings(candidates))

    findings.extend(_annualization_findings(value))

    return findings


def _target_return_wording_findings(
    *,
    normalized_quote: str,
    source_url: str | None,
    highest: float,
    return_type: ReturnType,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    unrelated = [marker for marker in UNRELATED_PERCENTAGE_MARKERS if marker in normalized_quote]

    if unrelated:
        findings.append(
            ValidationFinding(
                code=(ValidationCode.UNRELATED_PERCENTAGE_AS_TARGET_RETURN),
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The quoted text measures something that is not a return "
                    f"({', '.join(unrelated)}), so the percentage is not a "
                    "target return."
                ),
            )
        )

    historical = [marker for marker in HISTORICAL_RETURN_MARKERS if marker in normalized_quote]

    if historical and return_type not in {
        ReturnType.TARGET,
        ReturnType.EXPECTED,
        ReturnType.PREFERRED,
        ReturnType.HURDLE,
        ReturnType.GUARANTEED_MINIMUM,
    }:
        findings.append(
            ValidationFinding(
                code=(ValidationCode.HISTORICAL_RETURN_AS_TARGET_RETURN),
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The quoted text reports an achieved result "
                    f"({', '.join(historical)}) and the value is not stated "
                    "as a target, expected, preferred, hurdle or guaranteed "
                    "return."
                ),
            )
        )

    scenario = [marker for marker in KID_SCENARIO_MARKERS if marker in normalized_quote]

    from_kid = source_url is not None and _url_states_a_kid(source_url)

    if scenario and (from_kid or highest > TARGET_RETURN_KID_SUSPICIOUS):
        findings.append(
            ValidationFinding(
                code=ValidationCode.KID_SCENARIO_AS_TARGET_RETURN,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The quoted text is a performance scenario or a cost "
                    f"impact of a key information document ({', '.join(scenario)}), "
                    "which is a projection and not a target."
                ),
            )
        )
    elif from_kid and highest > TARGET_RETURN_KID_SUSPICIOUS:
        findings.append(
            ValidationFinding(
                code=ValidationCode.KID_SCENARIO_AS_TARGET_RETURN,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The value comes from a key information document and is "
                    "high enough to be a performance scenario."
                ),
            )
        )

    return findings


def _target_return_magnitude_findings(
    candidates: Sequence[float],
) -> list[ValidationFinding]:
    highest = max(candidates)

    if any(YEAR_LIKE_MINIMUM <= item <= YEAR_LIKE_MAXIMUM for item in candidates):
        return [
            ValidationFinding(
                code=ValidationCode.YEAR_CAPTURED_AS_PERCENTAGE,
                severity=ValidationSeverity.REJECT,
                detail=(
                    "The value lies in the range of a calendar year, so a year "
                    "was captured instead of a percentage."
                ),
            )
        ]

    if highest > TARGET_RETURN_IMPLAUSIBLE:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLE_TARGET_RETURN,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"A target return of {highest:g} % a year exceeds any realistic fund target."
                ),
            )
        ]

    if highest > TARGET_RETURN_HIGH:
        return [
            ValidationFinding(
                code=ValidationCode.UNUSUALLY_HIGH_TARGET_RETURN,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{highest:g} % a year is far above the usual band of "
                    "Czech qualified investor funds and may be a cumulative "
                    "or historical performance rather than a target."
                ),
            )
        ]

    if len(candidates) == 1 and candidates[0] == 0:
        return [
            ValidationFinding(
                code=ValidationCode.ZERO_TARGET_RETURN,
                severity=ValidationSeverity.REVIEW,
                detail="A target return of zero is not a meaningful fund target.",
            )
        ]

    return []


_KID_URL_MARKERS: Final[tuple[str, ...]] = (
    "kid",
    "priips",
    "kiid",
    "sdeleni-klicovych",
    "sdeleni_klicovych",
)


def _url_states_a_kid(
    url: str,
) -> bool:
    normalized = normalize_search_text(url)

    return any(marker in normalized for marker in _KID_URL_MARKERS)


def validate_horizon_wording(
    value: InvestmentHorizonValue,
    *,
    quote: str | None,
) -> list[ValidationFinding]:
    """
    Refuse a horizon whose source stated a bound the value threw away.

    "Investicni horizont: min. 3 roky" says three years is the least the
    fund will accept. Delivered as an exact three years it tells an
    investor the opposite of what the fund meant — that three years is
    the plan rather than the floor. One delivered output carried exactly
    that, and it was the extraction, not the source, that lost the word.
    """

    if not quote:
        return []

    normalized = normalize_search_text(quote)

    if classify_horizon_kind(normalized) is HorizonKind.MINIMUM:
        if value.kind is not HorizonKind.MINIMUM:
            return [
                ValidationFinding(
                    code=ValidationCode.HORIZON_BOUND_LOST,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        f"The source states the horizon as a minimum, and the "
                        f"value is stored as {value.kind.value!r}, so the "
                        "least the fund accepts reads as the period it "
                        "recommends."
                    ),
                )
            ]

        return []

    if states_a_horizon_range(normalized) and value.kind is not HorizonKind.RANGE:
        return [
            ValidationFinding(
                code=ValidationCode.HORIZON_BOUND_LOST,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The source states the horizon as a span of years and the "
                    f"value is stored as {value.kind.value!r}, so one end of "
                    "the span stands for the whole of it."
                ),
            )
        ]

    if any(marker in normalized for marker in HORIZON_MAXIMUM_MARKERS):
        return [
            ValidationFinding(
                code=ValidationCode.HORIZON_BOUND_LOST,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "The source states the horizon as an upper bound, which "
                    "the delivered value has no way to express."
                ),
            )
        ]

    return []


def validate_capital_metric_wording(
    *,
    metric_type: AumMetricType,
    quote: str | None,
) -> list[ValidationFinding]:
    """
    Refuse a capital figure whose evidence names a different quantity.

    Fund capital, net assets, a net asset value and equity are four
    different lines of the same statement, and a fund's own report names
    which one it is stating. One delivered output read "Fondovy kapital
    Spolecnosti dosahl ... 52 012 tis. Kc" and stored it as equity, while
    the same figure on the same day sat in the fund's own history as
    fund capital.
    """

    if not quote:
        return []

    stated = classify_capital_metric(normalize_search_text(quote))

    if stated is None or stated is metric_type:
        return []

    # The generic label is not evidence against a specific one: a report
    # writing "aktiva ve sprave" while the value is stored as the assets
    # of the fund says the same thing twice.
    if {stated, metric_type} <= {
        AumMetricType.ASSETS_UNDER_MANAGEMENT,
        AumMetricType.FUND_AUM,
    }:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.CAPITAL_METRIC_CONTRADICTS_EVIDENCE,
            severity=ValidationSeverity.REVIEW,
            detail=(
                f"The evidence names {stated.value!r} and the value is "
                f"stored as {metric_type.value!r}. The two are different "
                "lines of a statement and are not interchangeable."
            ),
        )
    ]


def validate_return_wording(
    value: TargetReturnValue,
    *,
    quote: str | None,
) -> list[ValidationFinding]:
    """
    Refuse a fixed rate read out of a rate that moves with a benchmark.

    "zhodnoceni 2TR + 1 % p.a." promises the two-week repo rate plus one
    point. Delivered as a target of 1 % a year it is not an approximation
    of that promise but a different and much smaller one, and nothing in
    the stored model can hold the reference rate.
    """

    if not quote:
        return []

    if value.value_percent_pa is None and value.minimum_percent_pa is None:
        return []

    normalized = normalize_search_text(quote)

    if not states_benchmark_linked_return(normalized):
        return []

    return [
        ValidationFinding(
            code=ValidationCode.BENCHMARK_RETURN_AS_FIXED_RATE,
            severity=ValidationSeverity.REJECT,
            detail=(
                "The source states a return measured against a reference "
                "rate, so the stored percentage is the spread alone and "
                "not a rate the fund targets."
            ),
        )
    ]


def _annualization_findings(
    value: TargetReturnValue,
) -> list[ValidationFinding]:
    """
    Refuse a rate stored per annum that no source ever called annual.

    Every percentage of this field is delivered in a property whose name
    ends in ``_percent_pa``. A reader takes that literally, so a figure
    the source only ever wrote as a bare "10 %" is being given a period
    it may not have. Twenty-one delivered target returns carried a
    percentage with an unknown annualization.
    """

    rates = (
        value.value_percent_pa,
        value.minimum_percent_pa,
        value.maximum_percent_pa,
    )

    if all(rate is None for rate in rates):
        return []

    if value.annualization is not Annualization.UNKNOWN:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.UNKNOWN_ANNUALIZATION_FOR_ANNUAL_RATE,
            severity=ValidationSeverity.REVIEW,
            detail=(
                "The target return is stored as a rate per annum, but "
                "nothing in the source establishes the period it covers, "
                "so it may be a return over the whole investment horizon."
            ),
        )
    ]


def validate_assets_under_management(
    value: AssetsUnderManagementValue,
    *,
    today: date,
) -> list[ValidationFinding]:
    """Check that a delivered assets figure describes this fund's assets."""

    findings: list[ValidationFinding] = []

    if value.currency not in ACCEPTED_CURRENCIES:
        findings.append(
            ValidationFinding(
                code=ValidationCode.UNSUPPORTED_CURRENCY,
                severity=ValidationSeverity.REVIEW,
                detail=(f"{value.currency} is not a currency this data set reports amounts in."),
            )
        )

    findings.extend(
        _capital_metric_findings(
            metric_type=value.metric_type,
            subject="value",
        )
    )

    findings.extend(
        _capital_magnitude_findings(
            amount=value.amount,
            currency=value.currency,
            metric_type=value.metric_type,
            label="The value",
        )
    )

    findings.extend(
        _as_of_findings(
            as_of=value.as_of,
            today=today,
        )
    )

    return findings


def _capital_metric_findings(
    *,
    metric_type: AumMetricType,
    subject: str,
) -> list[ValidationFinding]:
    if metric_type is AumMetricType.MANAGER_AUM:
        return [
            ValidationFinding(
                code=ValidationCode.MANAGER_AUM_AS_FUND_AUM,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"The {subject} reports assets of the manager or the "
                    "group, which are not the assets of this fund."
                ),
            )
        ]

    if metric_type is AumMetricType.STATUTORY_MINIMUM_CAPITAL:
        return [
            ValidationFinding(
                code=ValidationCode.STATUTORY_CAPITAL_AS_AUM,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"The {subject} reports the statutory minimum capital, "
                    "which is the floor the law requires and not what the "
                    "fund holds."
                ),
            )
        ]

    if metric_type is AumMetricType.REGISTERED_CAPITAL:
        return [
            ValidationFinding(
                code=ValidationCode.STATUTORY_CAPITAL_AS_AUM,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"The {subject} reports the registered capital of the "
                    "fund, which is a fixed founding amount and not its "
                    "assets under management."
                ),
            )
        ]

    return []


# The wording that attaches capital to a company. A fund's own report
# describes the companies it holds, and "vlastni kapital spolecnosti
# X s.r.o." reads exactly like the fund's own capital to a pattern that
# only looks for an amount.
_CAPITAL_SUBJECT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "fond",
        "fondu",
        "fonde",
        "fondem",
        "fondy",
        "podfond",
        "podfondu",
        "spolecnost",
        "spolecnosti",
        "fund",
        "subfund",
    }
)


# Tokens a legal form contributes to every company alike, so they never
# distinguish one company from another.
_COMPANY_FORM_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "spolecnost",
        "spolecnosti",
        "sro",
        "spol",
        "sicav",
        "investicni",
    }
)


def _identity_tokens(
    name: str,
) -> set[str]:
    """
    Return the words that distinguish one company from another.

    Both sides of the comparison are reduced the same way, which they
    were not: the fund kept only words of four letters or more while a
    candidate kept everything from three, so a three-letter brand prefix
    counted as a difference in one direction and a match in the other.

    Short words are dropped on purpose. A shared prefix like "EBM" is
    branding, not identity, and "EBM Partner a.s." is a different legal
    entity from "EBM Real Estate SICAV, a.s." — the capital of one is
    not the capital of the other.
    """

    return {
        token
        for token in re.findall(r"[a-z0-9]{4,}", fold_diacritics(name))
        if token not in _COMPANY_FORM_TOKENS
    }


def _written_amount_forms(
    amount: float,
) -> set[str]:
    """Return how an amount can appear in a Czech document."""

    forms: set[str] = set()

    for scale in (1, 1_000, 1_000_000, 1_000_000_000):
        if amount % scale:
            continue

        scaled = amount / scale

        if scaled <= 0 or scaled != int(scaled):
            continue

        grouped = f"{int(scaled):,}".replace(",", " ")

        forms.add(grouped)
        forms.add(grouped.replace(" ", " "))
        forms.add(grouped.replace(" ", ""))

        # A Czech annual report groups thousands with a full stop as
        # readily as with a space: "31.596 tis. Kc" is the same figure
        # as "31 596 tis. Kc", and one delivered value escaped this rule
        # only because of the separator its report happened to use.
        forms.add(grouped.replace(" ", "."))

    return forms


def validate_capital_attribution(
    *,
    amounts: Sequence[float],
    quote: str | None,
    fund_name: str,
) -> list[ValidationFinding]:
    """
    Refuse capital that the evidence attaches to a different company.

    A qualified investor fund reports the companies it holds, so its
    annual report is full of other companies' capital. One delivered
    fund carried 119 350 000 000 CZK of "assets", read from "snizen
    vlastni kapital spolecnosti MS Trnita 1 s.r.o. o 119 350 mil. Kc" —
    the conversion was right and the owner was not.

    The rule only speaks when the evidence is unambiguous: the amount has
    to be findable in the quote, another company has to stand between the
    start of the quote and that amount, and neither the fund nor a plain
    word for the fund may stand any closer to it. Anything less stays
    silent, because refusing a value needs better evidence than doubting
    one.
    """

    if not quote:
        return []

    plain = quote.replace(" ", " ").replace(" ", " ")

    fund_tokens = _identity_tokens(fund_name)

    for amount in amounts:
        positions = [
            plain.find(form) for form in _written_amount_forms(amount) if plain.find(form) >= 0
        ]

        if not positions:
            continue

        end = min(positions)

        owner = _nearest_other_company(
            text=plain[:end],
            fund_tokens=fund_tokens,
        )

        if owner is None:
            continue

        between = set(fold_diacritics(plain[owner[0] : end]).split())

        if between & _CAPITAL_SUBJECT_WORDS or between & fund_tokens:
            continue

        return [
            ValidationFinding(
                code=ValidationCode.CAPITAL_OF_ANOTHER_COMPANY,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"The evidence attaches {amount:,.0f} to "
                    f"{owner[1]!r}, which is not {fund_name}. The capital of "
                    "a company the fund holds is not the capital of the fund."
                ),
            )
        ]

    return []


def _nearest_other_company(
    *,
    text: str,
    fund_tokens: set[str],
) -> tuple[int, str] | None:
    """Return the end position and name of the last company that is not the fund."""

    from fundscraper.field_definitions import COMPANY_NAME_PATTERN

    found: tuple[int, str] | None = None

    for match in COMPANY_NAME_PATTERN.finditer(text):
        name = match.group("name").strip()

        tokens = _identity_tokens(name)

        if tokens and not (tokens & fund_tokens):
            found = (
                match.end(),
                name,
            )

    return found


def _capital_magnitude_findings(
    *,
    amount: float,
    currency: str,
    metric_type: AumMetricType,
    label: str,
) -> list[ValidationFinding]:
    if metric_type in NON_FUND_CAPITAL_METRICS:
        return []

    if amount < PER_SHARE_VALUE_LIMIT:
        return [
            ValidationFinding(
                code=ValidationCode.PER_SHARE_VALUE_AS_AUM,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{label} is {amount:,.4g} {currency}, which is the value "
                    "of a single investment share rather than the capital of "
                    "a fund."
                ),
            )
        ]

    if amount < AUM_IMPLAUSIBLE_AMOUNT:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLY_SMALL_AUM,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{label} is {amount:,.0f} {currency}, far below what a "
                    "fund of this kind holds. Czech statements report amounts "
                    "in thousands, so the multiplier was probably lost."
                ),
            )
        ]

    if amount > AUM_IMPLAUSIBLY_LARGE_AMOUNT:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLY_LARGE_AUM,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{label} is {amount:,.0f} {currency}, larger than the "
                    "whole Czech qualified investor sector, so a unit "
                    "multiplier was probably applied twice."
                ),
            )
        ]

    return []


def _as_of_findings(
    *,
    as_of: date,
    today: date,
) -> list[ValidationFinding]:
    if as_of > today:
        return [
            ValidationFinding(
                code=ValidationCode.FUTURE_AS_OF_DATE,
                severity=ValidationSeverity.REVIEW,
                detail=(f"The reporting date {as_of.isoformat()} lies in the future."),
            )
        ]

    if (today - as_of).days > AUM_MAXIMUM_AGE_DAYS:
        return [
            ValidationFinding(
                code=ValidationCode.STALE_AS_OF_DATE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The reporting date {as_of.isoformat()} is older than "
                    "five years, so the value is outdated."
                ),
            )
        ]

    return []


def _future_dated_findings(
    *,
    dates: Sequence[date],
    subject: str,
    today: date | None,
) -> list[ValidationFinding]:
    """
    Refuse a dated series that reports days that have not happened.

    A fund publishes what its assets were, not what they will be. An
    observation dated in the future is a projection read as a measurement
    or a date parsed from the wrong column, and one delivered series
    carried values for 2026-12-31 and 2027-12-31 as if they had been
    recorded.
    """

    moment = today or datetime.now(UTC).date()

    ahead = sorted({item for item in dates if item > moment})

    if not ahead:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.FUTURE_AS_OF_DATE,
            severity=ValidationSeverity.REVIEW,
            detail=(
                f"The {subject} reports "
                + ", ".join(item.isoformat() for item in ahead[:3])
                + ", which lie in the future, so they cannot be measurements."
            ),
        )
    ]


# A company name never opens with a date. Both Czech spellings appear in
# the delivered output: "4. 10. 2021 AVANT investicni spolecnost, a.s."
# from a statute effective-date line and "2025 investicni spolecnost"
# from a copyright footer.
_LEADING_DATE_PATTERN: Final = re.compile(
    r"""
    ^\s*
    (?:
        \d{1,2}\s*[./]\s*\d{1,2}\s*[./]\s*\d{4}    # 4. 10. 2021
        |
        (?:19|20)\d{2}                                # 2025
    )
    (?![\d])
    """,
    re.VERBOSE,
)


def validate_party(
    *,
    party: FundParty,
    expected_role: PartyRole,
    fund_name: str,
) -> list[ValidationFinding]:
    """Check that a manager or administrator names a real company."""

    # Imported here because the field definitions import nothing from
    # this module and a top-level import would still be a cycle risk as
    # the vocabulary grows.
    from fundscraper.field_definitions import clean_party_name, is_generic_company_name

    findings: list[ValidationFinding] = []

    name = party.name.strip()

    if not name:
        return [
            ValidationFinding(
                code=ValidationCode.MISSING_VALUE_COMPONENT,
                severity=ValidationSeverity.REVIEW,
                detail=f"The {expected_role.value} carries no company name.",
            )
        ]

    if is_generic_company_name(name):
        findings.append(
            ValidationFinding(
                code=ValidationCode.PARTY_NAME_NOT_SPECIFIC,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {expected_role.value} is stored as {name!r}, which "
                    "is a legal form and names no company."
                ),
            )
        )
    elif clean_party_name(name) is None:
        # The rule the extraction applies, applied again to what was
        # delivered. One output holds "3.1 Administraci Fondu provadi
        # Investicni spolecnost" as an administrator: a numbered clause
        # of a statute, kept whole because the sentence it names the
        # company in was not recognised as a sentence.
        findings.append(
            ValidationFinding(
                code=ValidationCode.PARTY_NAME_NOT_SPECIFIC,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {expected_role.value} is stored as {name!r}, which "
                    "is a line of a document rather than the name of a "
                    "company."
                ),
            )
        )

    if party.role is not expected_role:
        findings.append(
            ValidationFinding(
                code=ValidationCode.PARTY_ROLE_MISMATCH,
                severity=ValidationSeverity.REJECT,
                detail=(
                    f"The {expected_role.value} field carries a party of role {party.role.value!r}."
                ),
            )
        )

    if _LEADING_DATE_PATTERN.match(name):
        findings.append(
            ValidationFinding(
                code=ValidationCode.PARTY_NAME_CONTAINS_A_DATE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {expected_role.value} is stored as {name!r}, which "
                    "opens with a date. The line of a statute or a register "
                    "extract was captured together with the company it "
                    "names, so the stored name is not the legal name."
                ),
            )
        )

    tokens = _distinctive_tokens(fund_name)

    normalized_name = normalize_search_text(name)

    if tokens and all(token in normalized_name for token in tokens):
        findings.append(
            ValidationFinding(
                code=ValidationCode.PARTY_IS_THE_FUND_ITSELF,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {expected_role.value} repeats the name of the fund, "
                    "so the company acting for it was not identified."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Extended fields
# ---------------------------------------------------------------------------


def validate_capital_observations(
    observations: Sequence[CapitalObservation],
    *,
    today: date | None = None,
) -> list[ValidationFinding]:
    """Check a series of capital figures reported as the fund assets."""

    findings: list[ValidationFinding] = []

    findings.extend(
        _future_dated_findings(
            dates=[observation.as_of for observation in observations],
            subject="capital series",
            today=today,
        )
    )

    metrics = {observation.metric_type for observation in observations}

    for metric in sorted(
        metrics,
        key=lambda item: item.value,
    ):
        findings.extend(
            _capital_metric_findings(
                metric_type=metric,
                subject="series",
            )
        )

    for observation in observations:
        findings.extend(
            _capital_magnitude_findings(
                amount=observation.amount,
                currency=observation.currency,
                metric_type=observation.metric_type,
                label=(f"The observation of {observation.as_of.isoformat()}"),
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

    return _collapse(findings)


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
                severity=ValidationSeverity.CONFLICT,
                detail=(
                    f"Year {year}"
                    + (f" of class {share_class}" if share_class else "")
                    + " is reported with different results: "
                    + ", ".join(f"{value:g} %" for value in sorted(values))
                ),
            )
        )

    classes = {
        observation.share_class or ""
        for observation in observations
        if observation.series_type is ReturnSeriesType.CALENDAR_YEAR
    }

    if len(classes) > 1 and "" in classes:
        findings.append(
            ValidationFinding(
                code=ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "Some annual results name a share class and some do not, "
                    "so the unnamed ones may hold the results of a class they "
                    "do not identify."
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

    return _collapse(findings)


def validate_historical_series(
    series: Sequence[HistoricalValueSeries],
    *,
    today: date | None = None,
) -> list[ValidationFinding]:
    """Check that every value series measures one thing consistently."""

    findings: list[ValidationFinding] = []

    findings.extend(
        _future_dated_findings(
            dates=[observation.as_of for item in series for observation in item.observations],
            subject="value series",
            today=today,
        )
    )

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

    currencies_by_type: dict[str, set[str]] = defaultdict(set)

    for item in series:
        by_type[item.value_type.value].add(item.share_class or "")

        currencies_by_type[item.value_type.value].add(item.currency)

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

    for value_type, currencies in sorted(currencies_by_type.items()):
        if len(currencies) > 1:
            findings.append(
                ValidationFinding(
                    code=ValidationCode.MIXED_CURRENCIES_IN_SERIES,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        f"The {value_type} series are reported in "
                        f"{', '.join(sorted(currencies))}, so their values "
                        "cannot be read as one history."
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

        findings.extend(_per_share_series_findings(item))

    return _collapse(findings)


# The series types that measure the fund as a whole. A value per share
# read into one of them is the failure the audit has to name.
_FUND_LEVEL_SERIES_TYPES: Final[frozenset[HistoricalValueType]] = frozenset(
    {
        HistoricalValueType.FUND_NET_ASSETS,
        HistoricalValueType.FUND_CAPITAL,
        HistoricalValueType.AUM,
    }
)


def _per_share_series_findings(
    series: HistoricalValueSeries,
) -> list[ValidationFinding]:
    """
    Measure a fund-level series against the magnitudes a fund really has.

    The same two rungs the single assets figure is measured against, so a
    capital of 0.5 CZK and a capital of 24 245 CZK are both named, and
    named differently: the first is the price of one investment share,
    the second an annual report published "v tis. Kc" whose multiplier
    was never applied. Delivered series carried both.
    """

    if series.value_type not in _FUND_LEVEL_SERIES_TYPES:
        return []

    per_share = [
        observation
        for observation in series.observations
        if observation.value < PER_SHARE_VALUE_LIMIT
    ]

    if per_share:
        return [
            ValidationFinding(
                code=ValidationCode.PER_SHARE_VALUE_AS_AUM,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {series.value_type.value} series holds "
                    f"{len(per_share)} value(s) below "
                    f"{PER_SHARE_VALUE_LIMIT:,.0f} {series.currency}, such as "
                    f"{per_share[0].value:g}, which are values per investment "
                    "share rather than the capital of the fund."
                ),
            )
        ]

    small = [
        observation
        for observation in series.observations
        if observation.value < AUM_IMPLAUSIBLE_AMOUNT
    ]

    if small:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLY_SMALL_AUM,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {series.value_type.value} series holds "
                    f"{len(small)} value(s) below "
                    f"{AUM_IMPLAUSIBLE_AMOUNT:,.0f} {series.currency}, such as "
                    f"{small[0].value:g}, which is far below what a fund of "
                    "this kind holds. Czech statements report amounts in "
                    "thousands, so the multiplier was probably lost."
                ),
            )
        ]

    large = [
        observation
        for observation in series.observations
        if observation.value > AUM_IMPLAUSIBLY_LARGE_AMOUNT
    ]

    if large:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLY_LARGE_AUM,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {series.value_type.value} series holds "
                    f"{large[0].value:,.0f} {series.currency}, larger than the "
                    "whole Czech qualified investor sector, so a unit "
                    "multiplier was probably applied twice."
                ),
            )
        ]

    return []


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

    undated = 0

    for item in items:
        if item.published_at is None:
            undated += 1

        host = canonical_domain(str(item.url))

        if fund_host and host == fund_host:
            continue

        if item.source_type is NewsSourceType.THIRD_PARTY:
            findings.append(
                ValidationFinding(
                    code=ValidationCode.NEWS_FROM_UNOFFICIAL_SOURCE,
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

    if undated:
        findings.append(
            ValidationFinding(
                code=ValidationCode.NEWS_WITHOUT_DATE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"{undated} of {len(items)} items carry no publication "
                    "date, so their order and their age cannot be checked."
                ),
            )
        )

    return findings


def validate_fee_items(
    items: Sequence[FeeItem],
) -> list[ValidationFinding]:
    """Check fee values, their units and the ranges they were read from."""

    findings: list[ValidationFinding] = []

    for item in items:
        findings.extend(_fee_range_findings(item))

        findings.extend(_fee_magnitude_findings(item))

        findings.extend(_fee_unit_findings(item))

    return findings


def _fee_range_findings(
    item: FeeItem,
) -> list[ValidationFinding]:
    if item.minimum_rate_percent is None and item.maximum_rate_percent is None:
        if _basis_states_a_range(item):
            return [
                ValidationFinding(
                    code=ValidationCode.COLLAPSED_FEE_RANGE,
                    severity=ValidationSeverity.REVIEW,
                    detail=(
                        f"The {item.type.value} fee is written as a "
                        "range in its source, but only one number is stored."
                    ),
                )
            ]

        return []

    if (
        item.rate_percent is not None
        and item.maximum_rate_percent is not None
        and item.minimum_rate_percent is not None
        and item.minimum_rate_percent != item.maximum_rate_percent
        and item.maximum is not True
    ):
        return [
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
        ]

    return []


def _fee_magnitude_findings(
    item: FeeItem,
) -> list[ValidationFinding]:
    rate = item.rate_percent

    if rate is None:
        return []

    if rate > FEE_RATE_IMPLAUSIBLE:
        return [
            ValidationFinding(
                code=ValidationCode.IMPLAUSIBLE_FEE_RATE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"A {item.type.value} fee of {rate:g} % is above 100 % "
                    "and is probably an absolute amount captured as a "
                    "percentage."
                ),
            )
        ]

    limit = FEE_RATE_HIGH_BY_TYPE.get(
        item.type.value,
        FEE_RATE_IMPLAUSIBLE,
    )

    if rate > limit:
        return [
            ValidationFinding(
                code=ValidationCode.UNUSUALLY_HIGH_FEE_RATE,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"A {item.type.value} fee of {rate:g} % is far above the "
                    "usual band for this fee type and may be a different "
                    "figure of the source."
                ),
            )
        ]

    return []


# A currency written straight after a number, proving the figure is an
# amount and not a rate.
_AMOUNT_UNIT_PATTERN: Final = re.compile(
    r"(?P<number>\d+(?:[.,]\d+)?)\s*(?:kc|czk|eur|usd|€|\$)",
)

_RATE_UNIT_PATTERN: Final = re.compile(
    r"(?P<number>\d+(?:[.,]\d+)?)\s*%",
)


def _fee_unit_findings(
    item: FeeItem,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    if item.fixed_amount is not None and item.currency is None:
        findings.append(
            ValidationFinding(
                code=ValidationCode.MISSING_CURRENCY,
                severity=ValidationSeverity.REVIEW,
                detail=(f"The {item.type.value} fee has a fixed amount without a currency."),
            )
        )

    basis = normalize_search_text(
        " ".join(
            part
            for part in (
                item.basis,
                item.condition,
                item.details,
            )
            if part
        )
    )

    if not basis:
        return findings

    if item.fixed_amount is not None and _written_with(
        pattern=_RATE_UNIT_PATTERN,
        basis=basis,
        value=item.fixed_amount,
    ):
        findings.append(
            ValidationFinding(
                code=ValidationCode.FEE_AMOUNT_RATE_MISMATCH,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {item.type.value} fee stores {item.fixed_amount:g} "
                    "as a fixed amount, but the source writes that number as "
                    "a percentage."
                ),
            )
        )

    if (
        item.rate_percent is not None
        and item.rate_percent > 0
        and _written_with(
            pattern=_AMOUNT_UNIT_PATTERN,
            basis=basis,
            value=item.rate_percent,
        )
        and not _written_with(
            pattern=_RATE_UNIT_PATTERN,
            basis=basis,
            value=item.rate_percent,
        )
    ):
        findings.append(
            ValidationFinding(
                code=ValidationCode.FEE_AMOUNT_RATE_MISMATCH,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    f"The {item.type.value} fee stores {item.rate_percent:g} "
                    "as a percentage, but the source writes that number as an "
                    "amount in a currency."
                ),
            )
        )

    return findings


def _written_with(
    *,
    pattern: re.Pattern[str],
    basis: str,
    value: float,
) -> bool:
    for match in pattern.finditer(basis):
        try:
            written = float(match.group("number").replace(",", "."))
        except ValueError:
            continue

        if abs(written - value) < 1e-9:
            return True

    return False


# ---------------------------------------------------------------------------
# Fallback parser
# ---------------------------------------------------------------------------


def fallback_extraction_metadata(
    confidence: Confidence,
    *,
    placed_on_a_page: bool,
) -> tuple[Confidence, bool]:
    """
    Return what a value read from a rebuilt text is worth, and its review flag.

    This is the single place the rule is applied. ``validate_fallback_
    extraction`` below states the same rule as a check, so a value that
    reached the output another way is measured against it too.
    """

    if not placed_on_a_page:
        return (
            Confidence.LOW,
            True,
        )

    if confidence is Confidence.HIGH:
        return (
            Confidence.MEDIUM,
            True,
        )

    return (
        confidence,
        True,
    )


def validate_fallback_extraction(
    *,
    is_fallback: bool,
    confidence: Confidence,
    review_required: bool,
    placed_on_a_page: bool,
) -> list[ValidationFinding]:
    """
    Check the guarantees a value read from a rebuilt text has to carry.

    The AnyDoc experiment produced values that were perfectly plausible
    and simply wrong: a construction progress of 75 per cent read as a
    guaranteed minimum return, a value per share read as the assets of
    the fund. Rebuilding a page puts words next to each other that were
    never adjacent, so nothing measured this way may be delivered as a
    high-confidence value, and every such value goes in front of a
    reviewer.
    """

    if not is_fallback:
        return []

    findings: list[ValidationFinding] = []

    if not review_required:
        findings.append(
            ValidationFinding(
                code=ValidationCode.FALLBACK_PARSER_REVIEW_REQUIRED,
                severity=ValidationSeverity.REVIEW,
                detail=("A value read by the layout fallback is not marked for review."),
            )
        )

    if confidence is Confidence.HIGH:
        findings.append(
            ValidationFinding(
                code=ValidationCode.FALLBACK_PARSER_REVIEW_REQUIRED,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "A value read by the layout fallback is delivered with "
                    "high confidence, which a rebuilt text cannot support."
                ),
            )
        )

    if not placed_on_a_page and confidence is not Confidence.LOW:
        findings.append(
            ValidationFinding(
                code=ValidationCode.FALLBACK_PARSER_REVIEW_REQUIRED,
                severity=ValidationSeverity.REVIEW,
                detail=(
                    "A value read by the layout fallback could not be placed "
                    "on a page, so a reader cannot check it against the "
                    "document, yet its confidence was not reduced."
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Cross-field rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FundRecordView:
    """
    The delivered values of one fund that the cross-field rules read.

    Everything is optional: a rule that has nothing to compare simply
    produces nothing. Only relationships that can be checked without
    inventing a financial calculation are implemented.
    """

    fund_name: str = ""
    assets_under_management: AssetsUnderManagementValue | None = None
    aum_observations: tuple[CapitalObservation, ...] = ()
    historical_series: tuple[HistoricalValueSeries, ...] = ()
    annual_returns: tuple[AnnualReturnObservation, ...] = ()
    target_return: TargetReturnValue | None = None
    minimum_investment: MinimumInvestmentValue | None = None
    manager: FundParty | None = None
    administrator: FundParty | None = None
    fees: tuple[FeeItem, ...] = ()


def validate_cross_fields(
    record: FundRecordView,
) -> list[ValidationFinding]:
    """Check the relations between the delivered fields of one fund."""

    findings: list[ValidationFinding] = []

    findings.extend(_aum_against_history(record))

    findings.extend(_aum_superseded_by_history(record))

    findings.extend(_aum_against_per_share_values(record))

    findings.extend(_target_return_against_history(record))

    findings.extend(_party_roles(record))

    return findings


def _aum_superseded_by_history(
    record: FundRecordView,
) -> list[ValidationFinding]:
    """
    Refuse a current assets figure the fund's own history has outgrown.

    This field is what the fund holds now. One delivered output reported
    a net asset value of 125.7 million CZK for 31 December 2023 as the
    current assets of a fund whose own history already held 328 million
    for 31 December 2025 — both read from the fund's own newsletters,
    both fund-level, and the older one delivered.

    Only the fund's own dated observations are compared, and only ones of
    a fund-level metric, so nothing here depends on today's date or on
    how old a value is allowed to be. A figure is refused for being
    superseded, never for being old: the observation that supersedes it
    stays in ``aum_history`` and so does this one.
    """

    value = record.assets_under_management

    if value is None:
        return []

    newer = [
        observation
        for observation in record.aum_observations
        if observation.metric_type not in NON_FUND_CAPITAL_METRICS
        and observation.as_of > value.as_of
    ]

    if not newer:
        return []

    latest = max(newer, key=lambda observation: observation.as_of)

    return [
        ValidationFinding(
            code=ValidationCode.NEWER_AUM_OBSERVATION_EXISTS,
            severity=ValidationSeverity.REVIEW,
            detail=(
                f"The delivered assets are dated {value.as_of.isoformat()} "
                f"while the fund's own history holds "
                f"{latest.amount:,.0f} {latest.currency} "
                f"({latest.metric_type.value}) for "
                f"{latest.as_of.isoformat()}, so the current figure has "
                "been superseded."
            ),
        )
    ]


def _aum_against_history(
    record: FundRecordView,
) -> list[ValidationFinding]:
    """Compare the delivered assets with the dated series of the same fund."""

    value = record.assets_under_management

    if value is None or not record.aum_observations:
        return []

    # Only the same metric of the same date in the same currency is
    # comparable. Total assets and net assets of one day differ by the
    # liabilities of the fund, and calling that a conflict would report
    # arithmetic as an error.
    same_date = [
        observation
        for observation in record.aum_observations
        if observation.as_of == value.as_of
        and observation.currency == value.currency
        and observation.metric_type is value.metric_type
        and observation.metric_type not in NON_FUND_CAPITAL_METRICS
    ]

    for observation in same_date:
        larger = max(observation.amount, value.amount)

        if larger == 0:
            continue

        if abs(observation.amount - value.amount) / larger <= AUM_AGREEMENT_TOLERANCE:
            return []

    if not same_date:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.CONFLICTING_VALUES,
            severity=ValidationSeverity.CONFLICT,
            detail=(
                f"The delivered assets of {value.as_of.isoformat()} are "
                f"{value.amount:,.0f} {value.currency}, while the capital "
                "series reports "
                + ", ".join(f"{item.amount:,.0f}" for item in same_date[:3])
                + " for the same date."
            ),
            subject="assets_under_management",
        )
    ]


def _aum_against_per_share_values(
    record: FundRecordView,
) -> list[ValidationFinding]:
    """Check that the delivered assets are not a value per share."""

    value = record.assets_under_management

    if value is None:
        return []

    for series in record.historical_series:
        if series.value_type not in {
            HistoricalValueType.NAV_PER_SHARE,
            HistoricalValueType.INVESTMENT_SHARE_VALUE,
        }:
            continue

        for observation in series.observations:
            if observation.currency != value.currency:
                continue

            if abs(observation.value - value.amount) < 1e-6:
                return [
                    ValidationFinding(
                        code=ValidationCode.PER_SHARE_VALUE_AS_AUM,
                        severity=ValidationSeverity.REJECT,
                        detail=(
                            f"The delivered assets equal the "
                            f"{series.value_type.value} of "
                            f"{observation.as_of.isoformat()}, so a value per "
                            "investment share was reported as the assets of "
                            "the fund."
                        ),
                        subject="assets_under_management",
                    )
                ]

    return []


def _target_return_against_history(
    record: FundRecordView,
) -> list[ValidationFinding]:
    """Check that the target return is not a result the fund already had."""

    value = record.target_return

    if value is None or value.value_percent_pa is None:
        return []

    if value.return_type in {
        ReturnType.RANGE,
        ReturnType.NOT_PUBLISHED,
    }:
        return []

    matching = [
        observation
        for observation in record.annual_returns
        if observation.series_type is ReturnSeriesType.CALENDAR_YEAR
        and abs(observation.return_percent - value.value_percent_pa) < 1e-6
    ]

    if not matching:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.HISTORICAL_RETURN_AS_TARGET_RETURN,
            severity=ValidationSeverity.REVIEW,
            detail=(
                f"The stated {value.return_type.value} return of "
                f"{value.value_percent_pa:g} % equals the achieved result of "
                + ", ".join(str(item.year) for item in matching[:3])
                + ", so a past performance may have been read as a target."
            ),
            subject="target_return",
        )
    ]


def _party_roles(
    record: FundRecordView,
) -> list[ValidationFinding]:
    """
    Check that the manager and the administrator are told apart.

    Czech investment companies routinely act as both, and forty of the
    eighty-two funds that carry both parties name the same company twice.
    That is correct and is not reported. What is reported is the same
    company name under two different registration numbers, because then
    one of the two was read from the wrong sentence.
    """

    manager = record.manager

    administrator = record.administrator

    if manager is None or administrator is None:
        return []

    if _company_key(manager.name) != _company_key(administrator.name):
        return []

    if manager.ico is None or administrator.ico is None:
        return []

    if manager.ico == administrator.ico:
        return []

    return [
        ValidationFinding(
            code=ValidationCode.CONFLICTING_VALUES,
            severity=ValidationSeverity.CONFLICT,
            detail=(
                f"The manager and the administrator are both {manager.name!r} "
                f"but carry different registration numbers, {manager.ico} and "
                f"{administrator.ico}."
            ),
            subject="administrator",
        )
    ]


def _company_key(
    name: str,
) -> str:
    """Return a company name reduced to what identifies it."""

    return "".join(character for character in normalize_search_text(name) if character.isalnum())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def rejects(
    findings: Iterable[ValidationFinding],
) -> bool:
    """Return whether any finding refuses the value outright."""

    return any(
        finding.severity in {ValidationSeverity.REJECT, ValidationSeverity.CONFLICT}
        for finding in findings
    )


def worst_severity(
    findings: Iterable[ValidationFinding],
) -> ValidationSeverity | None:
    """Return the strongest severity among the findings, if there is one."""

    ranked = sorted(
        findings,
        key=lambda finding: SEVERITY_RANK[finding.severity],
    )

    return ranked[-1].severity if ranked else None


SEVERITY_RANK: Final[dict[ValidationSeverity, int]] = {
    ValidationSeverity.REVIEW: 0,
    ValidationSeverity.CONFLICT: 1,
    ValidationSeverity.REJECT: 2,
}


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


def _collapse(
    findings: Sequence[ValidationFinding],
) -> list[ValidationFinding]:
    """
    Keep one finding per code, the strongest and first of its kind.

    A series of twenty per-share observations states one defect, not
    twenty, and a report that repeats it twenty times hides the other
    nineteen problems of the fund.
    """

    best: dict[ValidationCode, ValidationFinding] = {}

    for finding in findings:
        current = best.get(finding.code)

        if current is None or SEVERITY_RANK[finding.severity] > SEVERITY_RANK[current.severity]:
            best[finding.code] = finding

    return [finding for finding in findings if best.get(finding.code) is finding]


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
