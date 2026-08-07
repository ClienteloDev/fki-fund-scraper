from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    """Base model used for all generated output data."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class FieldStatus(StrEnum):
    PENDING = "pending"
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"
    ERROR = "error"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionMethod(StrEnum):
    REGEX = "regex"
    GROUNDED = "grounded"
    TABLE = "table"
    HTML_SELECTOR = "html_selector"
    LLM = "llm"
    HYBRID = "hybrid"
    MANUAL = "manual"


class DocumentType(StrEnum):
    PRIIPS_KID = "priips_kid"
    STATUTE = "statute"
    SUBFUND_STATUTE = "subfund_statute"
    MEMORANDUM = "memorandum"
    ANNUAL_REPORT = "annual_report"
    HALF_YEAR_REPORT = "half_year_report"
    FINANCIAL_STATEMENTS = "financial_statements"
    FACTSHEET = "factsheet"
    INFOLETTER = "infoletter"
    MARKETING_PAGE = "marketing_page"
    REGISTER = "register"
    OTHER = "other"


class ReasonCode(StrEnum):
    NOT_PUBLICLY_DISCLOSED = "not_publicly_disclosed"
    NOT_QUANTIFIED = "not_quantified"
    SOURCE_NOT_FOUND = "source_not_found"
    SCOPE_MISMATCH = "scope_mismatch"
    CONFLICTING_VALUES = "conflicting_values"
    WEBSITE_UNREACHABLE = "website_unreachable"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    CAPTCHA_REQUIRED = "captcha_required"
    AUTHENTICATION_REQUIRED = "authentication_required"
    DOCUMENT_DOWNLOAD_FAILED = "document_download_failed"
    SCANNED_DOCUMENT_OCR_FAILED = "scanned_document_ocr_failed"
    UNSUPPORTED_DOCUMENT_FORMAT = "unsupported_document_format"
    ENTITY_NOT_MATCHED = "entity_not_matched"
    MULTIPLE_ENTITY_MATCHES = "multiple_entity_matches"
    MULTIPLE_SUBFUNDS = "multiple_subfunds"
    MULTIPLE_SHARE_CLASSES = "multiple_share_classes"
    SCOPE_AMBIGUOUS = "scope_ambiguous"
    CONFLICTING_SOURCES = "conflicting_sources"
    SOURCE_OUTDATED = "source_outdated"
    ONLY_MANAGER_LEVEL_DATA = "only_manager_level_data"
    ONLY_HISTORICAL_PERFORMANCE = "only_historical_performance"
    NOT_APPLICABLE = "not_applicable"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    PROCESSING_ERROR = "processing_error"


class SourceKind(StrEnum):
    """Who published a source, independent of the document format."""

    OFFICIAL_FUND_WEBSITE = "official_fund_website"
    MANAGER_WEBSITE = "manager_website"
    ADMINISTRATOR_WEBSITE = "administrator_website"
    REGULATORY_REGISTER = "regulatory_register"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


class PartyRole(StrEnum):
    MANAGER = "manager"
    ADMINISTRATOR = "administrator"
    DEPOSITARY = "depositary"
    AUDITOR = "auditor"


class ScopeType(StrEnum):
    FUND = "fund"
    SUBFUND = "subfund"
    SHARE_CLASS = "share_class"
    MANAGER = "manager"
    UNKNOWN = "unknown"


class EntityMatchStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceMetadata(StrictModel):
    url: HttpUrl
    document_type: DocumentType
    retrieved_at: datetime
    title: str | None = None
    published_at: date | None = None
    effective_at: date | None = None
    sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class Evidence(StrictModel):
    source: SourceMetadata
    quote: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    section: str | None = None


class ExtractionMetadata(StrictModel):
    method: ExtractionMethod
    confidence: Confidence
    review_required: bool = False


class DataScope(StrictModel):
    type: ScopeType
    fund_name: str | None = None
    subfund_name: str | None = None
    share_class_name: str | None = None
    isin: str | None = None


class MissingReason(StrictModel):
    code: ReasonCode
    detail: str = Field(min_length=1)


class SourceAttempt(StrictModel):
    url: HttpUrl
    retrieved_at: datetime
    outcome: ReasonCode
    document_type: DocumentType | None = None
    detail: str | None = None


class FieldResult[ValueT](StrictModel):
    status: FieldStatus
    value: ValueT | None = None
    raw_value: str | None = None
    scope: DataScope | None = None
    source: Evidence | None = None
    extraction: ExtractionMetadata | None = None
    reason: MissingReason | None = None
    attempted_sources: list[SourceAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status is FieldStatus.FOUND:
            if self.value is None:
                raise ValueError("found result must contain value")

            if not self.raw_value:
                raise ValueError("found result must contain raw_value")

            if self.source is None:
                raise ValueError("found result must contain source evidence")

            if self.extraction is None:
                raise ValueError("found result must contain extraction metadata")

            if self.reason is not None:
                raise ValueError("found result must not contain a missing reason")

            return self

        if self.status is FieldStatus.PENDING:
            if any(
                item is not None
                for item in (
                    self.value,
                    self.raw_value,
                    self.scope,
                    self.source,
                    self.extraction,
                    self.reason,
                )
            ):
                raise ValueError("pending result must not contain extracted data")

            return self

        if self.value is not None:
            raise ValueError("non-found result must not contain value")

        if self.reason is None:
            raise ValueError("non-found result must contain reason")

        return self


class HorizonKind(StrEnum):
    EXACT = "exact"
    MINIMUM = "minimum"
    RANGE = "range"
    TEXTUAL = "textual"


class InvestmentHorizonValue(StrictModel):
    """
    The holding period recommended for a fund.

    ``recommended_years`` stays the single representative number so that
    existing consumers keep working. A range reports its lower bound
    there, and "at least five years" reports five.
    """

    recommended_years: float = Field(gt=0)

    kind: HorizonKind = HorizonKind.EXACT

    minimum_years: float | None = Field(default=None, gt=0)

    maximum_years: float | None = Field(default=None, gt=0)

    wording: str | None = None

    @model_validator(mode="after")
    def validate_horizon(self) -> Self:
        if self.kind is HorizonKind.RANGE:
            if self.minimum_years is None or self.maximum_years is None:
                raise ValueError("a horizon range requires both bounds")

            if self.minimum_years > self.maximum_years:
                raise ValueError("minimum horizon must not exceed maximum horizon")

        return self


class MinimumInvestmentKind(StrEnum):
    INITIAL_SUBSCRIPTION = "initial_subscription"
    SUBSEQUENT_SUBSCRIPTION = "subsequent_subscription"
    SHARE_CLASS_MINIMUM = "share_class_minimum"
    LEGAL_THRESHOLD = "legal_threshold"
    OTHER = "other"


class ValueOrigin(StrEnum):
    """Whether a value was published by the source or derived from law."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class InferenceType(StrEnum):
    LEGAL_DEFAULT = "legal_default"
    REGULATORY_THRESHOLD = "regulatory_threshold"
    MANAGER_POLICY = "manager_policy"


class InferenceReference(StrictModel):
    """The legal ground of a value that no source states explicitly."""

    inference_type: InferenceType
    legal_basis: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=2)
    effective_date: date


class MinimumInvestmentValue(StrictModel):
    amount: float = Field(ge=0)
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    kind: MinimumInvestmentKind
    condition: str | None = None

    share_class: str | None = None

    origin: ValueOrigin = ValueOrigin.EXPLICIT

    inference: InferenceReference | None = None

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if self.origin is ValueOrigin.INFERRED and self.inference is None:
            raise ValueError("an inferred minimum investment requires its legal basis")

        if self.origin is ValueOrigin.EXPLICIT and self.inference is not None:
            raise ValueError("an explicit minimum investment must not carry an inference")

        return self


class ReturnType(StrEnum):
    """Which return concept a stated percentage describes."""

    EXPECTED = "expected"
    TARGET = "target"
    GUARANTEED_MINIMUM = "guaranteed_minimum"
    PREFERRED = "preferred"
    HURDLE = "hurdle"
    RANGE = "range"
    OTHER = "other"
    NOT_PUBLISHED = "not_published"


class Annualization(StrEnum):
    PER_ANNUM = "per_annum"
    CUMULATIVE = "cumulative"
    PERIOD = "period"
    UNKNOWN = "unknown"


class TargetReturnValue(StrictModel):
    value_percent_pa: float | None = None
    minimum_percent_pa: float | None = None
    maximum_percent_pa: float | None = None
    condition: str | None = None

    return_type: ReturnType = ReturnType.TARGET

    annualization: Annualization = Annualization.PER_ANNUM

    period: str | None = None

    share_class: str | None = None

    subfund: str | None = None

    @model_validator(mode="after")
    def validate_return_values(self) -> Self:
        values = (
            self.value_percent_pa,
            self.minimum_percent_pa,
            self.maximum_percent_pa,
        )

        if all(value is None for value in values):
            raise ValueError("target return must contain an exact value or a range")

        if (
            self.minimum_percent_pa is not None
            and self.maximum_percent_pa is not None
            and self.minimum_percent_pa > self.maximum_percent_pa
        ):
            raise ValueError("minimum target return must not exceed maximum target return")

        return self


class FeeType(StrEnum):
    ENTRY = "entry"
    MANAGEMENT = "management"
    PERFORMANCE = "performance"
    EXIT = "exit"
    ADMINISTRATION = "administration"
    DEPOSITARY = "depositary"
    TRANSACTION = "transaction"
    ONGOING = "ongoing"
    OTHER = "other"


class FeeFrequency(StrEnum):
    ONE_OFF = "one_off"
    ANNUAL = "annual"
    MONTHLY = "monthly"
    PER_TRANSACTION = "per_transaction"
    CONDITIONAL = "conditional"
    OTHER = "other"


class FeeTierBasis(StrEnum):
    """What decides which tier of a fee applies."""

    HOLDING_PERIOD = "holding_period"
    INVESTMENT_AMOUNT = "investment_amount"
    SHARE_CLASS = "share_class"
    DISTRIBUTOR = "distributor"
    OTHER = "other"


class FeeTier(StrictModel):
    """
    One row of a conditional fee table.

    An exit fee of ten per cent in the first year and nothing after three
    years is three tiers, never one number.
    """

    basis: FeeTierBasis

    from_months: int | None = Field(default=None, ge=0)
    to_months: int | None = Field(default=None, ge=0)

    from_amount: float | None = Field(default=None, ge=0)
    to_amount: float | None = Field(default=None, ge=0)

    share_class: str | None = None
    distributor: str | None = None

    rate_percent: float | None = Field(default=None, ge=0)
    fixed_amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{3}$",
    )

    condition: str | None = None

    @model_validator(mode="after")
    def validate_tier(self) -> Self:
        if self.rate_percent is None and self.fixed_amount is None:
            raise ValueError("a fee tier requires a rate or a fixed amount")

        if self.fixed_amount is not None and self.currency is None:
            raise ValueError("a fixed tier amount requires currency")

        if (
            self.from_months is not None
            and self.to_months is not None
            and self.from_months > self.to_months
        ):
            raise ValueError("tier month range must not be inverted")

        return self


class FeeItem(StrictModel):
    type: FeeType
    rate_percent: float | None = Field(default=None, ge=0)
    fixed_amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{3}$",
    )
    frequency: FeeFrequency | None = None
    maximum: bool | None = None
    basis: str | None = None
    condition: str | None = None

    minimum_rate_percent: float | None = Field(default=None, ge=0)
    maximum_rate_percent: float | None = Field(default=None, ge=0)

    tiers: list[FeeTier] = Field(default_factory=list)

    negotiable: bool | None = None

    # Kept verbatim when the wording cannot be normalized safely.
    details: str | None = None

    @model_validator(mode="after")
    def validate_fee_value(self) -> Self:
        if (
            self.rate_percent is None
            and self.fixed_amount is None
            and not self.tiers
            and not self.condition
            and not self.details
        ):
            raise ValueError("fee must contain a rate, fixed amount, tier, condition or details")

        if (
            self.minimum_rate_percent is not None
            and self.maximum_rate_percent is not None
            and self.minimum_rate_percent > self.maximum_rate_percent
        ):
            raise ValueError("minimum fee rate must not exceed maximum fee rate")

        if self.fixed_amount is not None and self.currency is None:
            raise ValueError("fixed fee amount requires currency")

        return self


class FeeCollection(StrictModel):
    items: list[FeeItem] = Field(min_length=1)


class AumMetricType(StrEnum):
    """
    Which capital figure of a fund a value describes.

    Only the fund-level members may populate the assets-under-management
    field. Registered capital, the statutory minimum and the assets of
    the manager are stored under their own metric and never as the assets
    of the fund.
    """

    ASSETS_TOTAL = "assets_total"
    NET_ASSETS = "net_assets"
    NAV = "nav"
    EQUITY = "equity"
    FUND_AUM = "fund_aum"
    SUBFUND_AUM = "subfund_aum"
    ASSETS_UNDER_MANAGEMENT = "assets_under_management"
    FUND_CAPITAL = "fund_capital"
    REGISTERED_CAPITAL = "registered_capital"
    STATUTORY_MINIMUM_CAPITAL = "statutory_minimum_capital"
    MANAGER_AUM = "manager_aum"
    OTHER = "other"


# The metrics that describe the assets of the fund itself.
FUND_LEVEL_AUM_METRICS: frozenset[AumMetricType] = frozenset(
    {
        AumMetricType.ASSETS_UNDER_MANAGEMENT,
        AumMetricType.FUND_AUM,
        AumMetricType.SUBFUND_AUM,
        AumMetricType.NET_ASSETS,
        AumMetricType.NAV,
        AumMetricType.ASSETS_TOTAL,
        AumMetricType.EQUITY,
        AumMetricType.FUND_CAPITAL,
    }
)


class AssetsUnderManagementValue(StrictModel):
    amount: float = Field(ge=0)
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    metric_type: AumMetricType
    as_of: date


class FundParty(StrictModel):
    """A company acting for the fund, such as its manager."""

    role: PartyRole
    name: str = Field(min_length=1)
    legal_name: str | None = None
    ico: str | None = Field(
        default=None,
        pattern=r"^\d{8}$",
    )
    web: HttpUrl | None = None


class CapitalObservation(StrictModel):
    """One dated capital figure of a fund."""

    amount: float = Field(ge=0)
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    metric_type: AumMetricType
    as_of: date

    share_class: str | None = None
    scope: DataScope | None = None
    source: Evidence | None = None
    extraction: ExtractionMetadata | None = None


class AumHistory(StrictModel):
    """The assets of one fund over time."""

    observations: list[CapitalObservation] = Field(min_length=1)


class ReturnSeriesType(StrEnum):
    """Which period a reported performance covers."""

    CALENDAR_YEAR = "calendar_year"
    YEAR_TO_DATE = "year_to_date"
    ROLLING_12M = "rolling_12m"
    CUMULATIVE = "cumulative"
    ANNUALIZED_MULTI_YEAR = "annualized_multi_year"
    KID_SCENARIO = "kid_scenario"


class AnnualReturnObservation(StrictModel):
    """The performance of one fund in one calendar year."""

    year: int = Field(ge=1900, le=2100)
    return_percent: float
    series_type: ReturnSeriesType = ReturnSeriesType.CALENDAR_YEAR

    share_class: str | None = None
    currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{3}$",
    )

    source: Evidence | None = None
    extraction: ExtractionMetadata | None = None


class AnnualReturnHistory(StrictModel):
    observations: list[AnnualReturnObservation] = Field(min_length=1)


class HistoricalValueType(StrEnum):
    NAV_PER_SHARE = "nav_per_share"
    INVESTMENT_SHARE_VALUE = "investment_share_value"
    FUND_NET_ASSETS = "fund_net_assets"
    FUND_CAPITAL = "fund_capital"
    AUM = "aum"


class SeriesFrequency(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


class HistoricalValueObservation(StrictModel):
    as_of: date
    value: float
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )

    source: Evidence | None = None
    extraction: ExtractionMetadata | None = None


class HistoricalValueSeries(StrictModel):
    """
    One measured quantity of one share class over time.

    The identity of the series lives on the series itself, so values of
    different metrics, classes or currencies cannot end up mixed.
    """

    value_type: HistoricalValueType
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    frequency: SeriesFrequency = SeriesFrequency.IRREGULAR

    share_class: str | None = None
    unit: str | None = None

    observations: list[HistoricalValueObservation] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_series(self) -> Self:
        dates = [observation.as_of for observation in self.observations]

        if len(dates) != len(set(dates)):
            raise ValueError("a value series must not contain duplicate dates")

        if any(observation.currency != self.currency for observation in self.observations):
            raise ValueError("every observation must use the currency of its series")

        return self


class HistoricalValueCollection(StrictModel):
    series: list[HistoricalValueSeries] = Field(min_length=1)


class NewsSourceType(StrEnum):
    OFFICIAL_FUND = "official_fund"
    MANAGER = "manager"
    ADMINISTRATOR = "administrator"
    THIRD_PARTY = "third_party"


class FundNewsItem(StrictModel):
    title: str = Field(min_length=1)
    url: HttpUrl
    source_domain: str = Field(min_length=1)
    source_type: NewsSourceType

    published_at: date | None = None
    summary: str | None = None

    relation_confidence: Confidence = Confidence.LOW


class FundNewsCollection(StrictModel):
    items: list[FundNewsItem] = Field(min_length=1)


class FundIdentity(StrictModel):
    ico: str | None = Field(
        default=None,
        pattern=r"^\d{8}$",
    )
    legal_name: str | None = None
    match_status: EntityMatchStatus = EntityMatchStatus.UNVERIFIED
    confidence: Confidence | None = None


class ProcessingMetadata(StrictModel):
    status: ProcessingStatus = ProcessingStatus.PENDING
    updated_at: datetime
    warnings: list[str] = Field(default_factory=list)


class FundOutput(StrictModel):
    fund_id: str = Field(pattern=r"^fund_[0-9a-f]{16}$")
    name: str = Field(min_length=1)
    web: str = Field(min_length=1)

    identity: FundIdentity

    investment_horizon: FieldResult[InvestmentHorizonValue]
    minimum_investment: FieldResult[MinimumInvestmentValue]
    target_return: FieldResult[TargetReturnValue]
    fees: FieldResult[FeeCollection]
    assets_under_management: FieldResult[AssetsUnderManagementValue]

    # Added in schema version 3. Every field defaults to pending, so an
    # output written before it existed still loads unchanged.
    manager: FieldResult[FundParty] = Field(
        default_factory=lambda: FieldResult[FundParty](status=FieldStatus.PENDING),
    )
    administrator: FieldResult[FundParty] = Field(
        default_factory=lambda: FieldResult[FundParty](status=FieldStatus.PENDING),
    )
    aum_history: FieldResult[AumHistory] = Field(
        default_factory=lambda: FieldResult[AumHistory](status=FieldStatus.PENDING),
    )
    annual_returns: FieldResult[AnnualReturnHistory] = Field(
        default_factory=lambda: FieldResult[AnnualReturnHistory](status=FieldStatus.PENDING),
    )
    historical_values: FieldResult[HistoricalValueCollection] = Field(
        default_factory=lambda: FieldResult[HistoricalValueCollection](status=FieldStatus.PENDING),
    )
    news: FieldResult[FundNewsCollection] = Field(
        default_factory=lambda: FieldResult[FundNewsCollection](status=FieldStatus.PENDING),
    )

    processing: ProcessingMetadata
