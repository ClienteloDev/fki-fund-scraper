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


class InvestmentHorizonValue(StrictModel):
    recommended_years: float = Field(gt=0)


class MinimumInvestmentKind(StrEnum):
    INITIAL_SUBSCRIPTION = "initial_subscription"
    SUBSEQUENT_SUBSCRIPTION = "subsequent_subscription"
    LEGAL_THRESHOLD = "legal_threshold"
    OTHER = "other"


class MinimumInvestmentValue(StrictModel):
    amount: float = Field(ge=0)
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    kind: MinimumInvestmentKind
    condition: str | None = None


class TargetReturnValue(StrictModel):
    value_percent_pa: float | None = None
    minimum_percent_pa: float | None = None
    maximum_percent_pa: float | None = None
    condition: str | None = None

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

    @model_validator(mode="after")
    def validate_fee_value(self) -> Self:
        if self.rate_percent is None and self.fixed_amount is None and not self.condition:
            raise ValueError("fee must contain a rate, fixed amount, or condition")

        if self.fixed_amount is not None and self.currency is None:
            raise ValueError("fixed fee amount requires currency")

        return self


class FeeCollection(StrictModel):
    items: list[FeeItem] = Field(min_length=1)


class AumMetricType(StrEnum):
    ASSETS_TOTAL = "assets_total"
    NET_ASSETS = "net_assets"
    NAV = "nav"
    EQUITY = "equity"
    FUND_AUM = "fund_aum"
    SUBFUND_AUM = "subfund_aum"
    MANAGER_AUM = "manager_aum"
    OTHER = "other"


class AssetsUnderManagementValue(StrictModel):
    amount: float = Field(ge=0)
    currency: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$",
    )
    metric_type: AumMetricType
    as_of: date


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

    processing: ProcessingMetadata
