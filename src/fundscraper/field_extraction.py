from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from pydantic import HttpUrl

from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import ParsedDocument
from fundscraper.html_discovery import normalize_search_text
from fundscraper.output_models import (
    AssetsUnderManagementValue,
    AumMetricType,
    Confidence,
    DataScope,
    DocumentType,
    Evidence,
    ExtractionMetadata,
    ExtractionMethod,
    FeeCollection,
    FeeFrequency,
    FeeItem,
    FeeType,
    FieldResult,
    FieldStatus,
    InvestmentHorizonValue,
    MinimumInvestmentKind,
    MinimumInvestmentValue,
    MissingReason,
    ReasonCode,
    ScopeType,
    SourceAttempt,
    SourceMetadata,
    TargetReturnValue,
)

NUMBER_PATTERN: Final = (
    r"\d{1,3}(?:[ .]\d{3})*(?:[,.]\d+)?"
    r"|\d+(?:[,.]\d+)?"
)


HORIZON_PATTERN = re.compile(
    r"""
    (?:
        doporucen[ay]?\s+
        (?:
            investicni\s+
        )?
        (?:
            horizont
            |
            doba\s+drzeni
        )
        |
        investicni\s+horizont
        |
        minimalni\s+doba\s+investice
        |
        recommended\s+holding\s+period
        |
        investment\s+horizon
    )
    .{0,100}?
    (?P<years>\d+(?:[,.]\d+)?)
    \s*
    (?:
        let
        |
        roku
        |
        roky
        |
        years?
        |
        yrs?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


MINIMUM_INVESTMENT_PATTERN = re.compile(
    rf"""
    (?:
        minimalni
        |
        nejnizsi
        |
        minimum
        |
        pocatecni
        |
        initial
    )
    .{{0,40}}?
    (?:
        investice
        |
        vklad
        |
        upis
        |
        subscription
        |
        investment
    )
    .{{0,100}}?
    (?P<amount>{NUMBER_PATTERN})
    \s*
    (?P<currency>czk|kc|eur|usd)
    """,
    re.IGNORECASE | re.VERBOSE,
)


TARGET_RETURN_RANGE_PATTERN = re.compile(
    rf"""
    (?:
        cilov[ay]
        |
        ocekavan[ay]
        |
        predpokladan[ay]
        |
        target
        |
        expected
        |
        anticipated
    )
    .{{0,50}}?
    (?:
        vynos
        |
        zhodnoceni
        |
        return
    )
    .{{0,80}}?
    (?P<minimum>{NUMBER_PATTERN})
    \s*
    (?:
        -
        |
        az
        |
        to
    )
    \s*
    (?P<maximum>{NUMBER_PATTERN})
    \s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)


TARGET_RETURN_EXACT_PATTERN = re.compile(
    rf"""
    (?:
        cilov[ay]
        |
        ocekavan[ay]
        |
        predpokladan[ay]
        |
        target
        |
        expected
        |
        anticipated
    )
    .{{0,50}}?
    (?:
        vynos
        |
        zhodnoceni
        |
        return
    )
    .{{0,80}}?
    (?P<value>{NUMBER_PATTERN})
    \s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)


PERCENT_RANGE_PATTERN = re.compile(
    rf"""
    (?P<minimum>{NUMBER_PATTERN})
    \s*
    (?:
        -
        |
        az
        |
        to
    )
    \s*
    (?P<maximum>{NUMBER_PATTERN})
    \s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)


PERCENT_PATTERN = re.compile(
    rf"""
    (?P<value>{NUMBER_PATTERN})
    \s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)


MONEY_PATTERN = re.compile(
    rf"""
    (?P<amount>{NUMBER_PATTERN})
    \s*
    (?P<multiplier>
        tis
        |
        tisic
        |
        mil
        |
        milion
        |
        milionu
        |
        million
        |
        mld
        |
        miliarda
        |
        miliard
        |
        billion
    )?
    \.?
    \s*
    (?P<currency>czk|kc|eur|usd)
    """,
    re.IGNORECASE | re.VERBOSE,
)


DATE_NUMERIC_PATTERN = re.compile(
    r"""
    (?P<day>\d{1,2})
    \s*[./-]\s*
    (?P<month>\d{1,2})
    \s*[./-]\s*
    (?P<year>20\d{2})
    """,
    re.VERBOSE,
)


DATE_ISO_PATTERN = re.compile(
    r"""
    (?P<year>20\d{2})
    -
    (?P<month>\d{2})
    -
    (?P<day>\d{2})
    """,
    re.VERBOSE,
)


MONTHS: Final[dict[str, int]] = {
    "january": 1,
    "leden": 1,
    "ledna": 1,
    "february": 2,
    "unor": 2,
    "unora": 2,
    "march": 3,
    "brezen": 3,
    "brezna": 3,
    "april": 4,
    "duben": 4,
    "dubna": 4,
    "may": 5,
    "kveten": 5,
    "kvetna": 5,
    "june": 6,
    "cerven": 6,
    "cervna": 6,
    "july": 7,
    "cervenec": 7,
    "cervence": 7,
    "august": 8,
    "srpen": 8,
    "srpna": 8,
    "september": 9,
    "zari": 9,
    "october": 10,
    "rijen": 10,
    "rijna": 10,
    "november": 11,
    "listopad": 11,
    "listopadu": 11,
    "december": 12,
    "prosinec": 12,
    "prosince": 12,
}


DATE_TEXT_PATTERN = re.compile(
    r"""
    (?P<day>\d{1,2})
    \s+
    (?P<month>[a-z]+)
    \s+
    (?P<year>20\d{2})
    """,
    re.IGNORECASE | re.VERBOSE,
)


FEE_KEYWORDS: Final[
    tuple[
        tuple[
            FeeType,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        FeeType.PERFORMANCE,
        (
            "vykonnostni poplatek",
            "vykonnostni odmena",
            "performance fee",
            "performance remuneration",
        ),
    ),
    (
        FeeType.MANAGEMENT,
        (
            "poplatek za obhospodarovani",
            "odmena za obhospodarovani",
            "management fee",
            "management remuneration",
        ),
    ),
    (
        FeeType.ENTRY,
        (
            "vstupni poplatek",
            "vstupni naklady",
            "entry fee",
            "subscription fee",
        ),
    ),
    (
        FeeType.EXIT,
        (
            "vystupni poplatek",
            "vystupni naklady",
            "exit fee",
            "redemption fee",
        ),
    ),
    (
        FeeType.ONGOING,
        (
            "prubezne naklady",
            "ongoing costs",
            "ongoing charges",
        ),
    ),
    (
        FeeType.ADMINISTRATION,
        (
            "administracni poplatek",
            "administration fee",
        ),
    ),
    (
        FeeType.DEPOSITARY,
        (
            "odmena depozitare",
            "poplatek depozitari",
            "depositary fee",
        ),
    ),
)


HORIZON_DOCUMENT_PRIORITY: Final = {
    DocumentType.PRIIPS_KID: 80,
    DocumentType.STATUTE: 70,
    DocumentType.SUBFUND_STATUTE: 75,
    DocumentType.MEMORANDUM: 65,
    DocumentType.FACTSHEET: 50,
    DocumentType.MARKETING_PAGE: 20,
}


MINIMUM_DOCUMENT_PRIORITY: Final = {
    DocumentType.STATUTE: 80,
    DocumentType.SUBFUND_STATUTE: 85,
    DocumentType.MEMORANDUM: 80,
    DocumentType.PRIIPS_KID: 55,
    DocumentType.FACTSHEET: 50,
    DocumentType.MARKETING_PAGE: 30,
}


TARGET_DOCUMENT_PRIORITY: Final = {
    DocumentType.MEMORANDUM: 80,
    DocumentType.FACTSHEET: 75,
    DocumentType.INFOLETTER: 60,
    DocumentType.MARKETING_PAGE: 40,
    DocumentType.STATUTE: 40,
}


FEE_DOCUMENT_PRIORITY: Final = {
    DocumentType.PRIIPS_KID: 85,
    DocumentType.STATUTE: 80,
    DocumentType.SUBFUND_STATUTE: 85,
    DocumentType.MEMORANDUM: 70,
    DocumentType.FACTSHEET: 60,
    DocumentType.MARKETING_PAGE: 30,
}


AUM_DOCUMENT_PRIORITY: Final = {
    DocumentType.ANNUAL_REPORT: 90,
    DocumentType.FINANCIAL_STATEMENTS: 90,
    DocumentType.HALF_YEAR_REPORT: 80,
    DocumentType.FACTSHEET: 70,
    DocumentType.INFOLETTER: 60,
    DocumentType.MARKETING_PAGE: 30,
}


@dataclass(frozen=True, slots=True)
class ExtractionDocument:
    record: ParsedDocumentRecord
    document: ParsedDocument


@dataclass(frozen=True, slots=True)
class TextWindow:
    document: ExtractionDocument
    page_number: int | None
    quote: str
    normalized: str


@dataclass(frozen=True, slots=True)
class Candidate[ValueT]:
    value: ValueT
    raw_value: str
    quote: str
    page_number: int | None
    document: ExtractionDocument
    score: int


@dataclass(frozen=True, slots=True)
class ExtractedFundFields:
    investment_horizon: FieldResult[InvestmentHorizonValue]

    minimum_investment: FieldResult[MinimumInvestmentValue]

    target_return: FieldResult[TargetReturnValue]

    fees: FieldResult[FeeCollection]

    assets_under_management: FieldResult[AssetsUnderManagementValue]


def extract_fund_fields(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> ExtractedFundFields:
    """Extract all supported fund fields from parsed documents."""

    return ExtractedFundFields(
        investment_horizon=extract_investment_horizon(
            fund_name=fund_name,
            documents=documents,
        ),
        minimum_investment=extract_minimum_investment(
            fund_name=fund_name,
            documents=documents,
        ),
        target_return=extract_target_return(
            fund_name=fund_name,
            documents=documents,
        ),
        fees=extract_fees(
            fund_name=fund_name,
            documents=documents,
        ),
        assets_under_management=extract_aum(
            fund_name=fund_name,
            documents=documents,
        ),
    )


def extract_investment_horizon(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[InvestmentHorizonValue]:
    candidates: list[Candidate[InvestmentHorizonValue]] = []

    for window in _iter_windows(documents):
        match = HORIZON_PATTERN.search(window.normalized)

        if match is None:
            continue

        years = _parse_number(match.group("years"))

        if not 0 < years <= 100:
            continue

        score = (
            _document_priority(
                window.document,
                HORIZON_DOCUMENT_PRIORITY,
            )
            + 70
        )

        if "doporucen" in window.normalized:
            score += 15

        candidates.append(
            Candidate(
                value=InvestmentHorizonValue(recommended_years=years),
                raw_value=window.quote,
                quote=window.quote,
                page_number=window.page_number,
                document=window.document,
                score=score,
            )
        )

    return _candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No quantified recommended investment horizon was found in the parsed public sources."
        ),
    )


def extract_minimum_investment(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[MinimumInvestmentValue]:
    candidates: list[Candidate[MinimumInvestmentValue]] = []

    for window in _iter_windows(documents):
        match = MINIMUM_INVESTMENT_PATTERN.search(window.normalized)

        if match is None:
            continue

        amount = _parse_number(match.group("amount"))

        currency = _normalize_currency(match.group("currency"))

        if amount < 0:
            continue

        kind = (
            MinimumInvestmentKind.LEGAL_THRESHOLD
            if any(
                keyword in window.normalized
                for keyword in (
                    "zakon",
                    "kvalifikovany investor",
                    "legal threshold",
                )
            )
            else MinimumInvestmentKind.INITIAL_SUBSCRIPTION
        )

        score = (
            _document_priority(
                window.document,
                MINIMUM_DOCUMENT_PRIORITY,
            )
            + 70
        )

        candidates.append(
            Candidate(
                value=MinimumInvestmentValue(
                    amount=amount,
                    currency=currency,
                    kind=kind,
                ),
                raw_value=window.quote,
                quote=window.quote,
                page_number=window.page_number,
                document=window.document,
                score=score,
            )
        )

    return _candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=("No quantified minimum investment was found in the parsed public sources."),
    )


def extract_target_return(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[TargetReturnValue]:
    candidates: list[Candidate[TargetReturnValue]] = []

    for window in _iter_windows(documents):
        range_match = TARGET_RETURN_RANGE_PATTERN.search(window.normalized)

        if range_match is not None:
            minimum = _parse_number(range_match.group("minimum"))

            maximum = _parse_number(range_match.group("maximum"))

            if minimum <= maximum:
                score = (
                    _document_priority(
                        window.document,
                        TARGET_DOCUMENT_PRIORITY,
                    )
                    + 80
                )

                candidates.append(
                    Candidate(
                        value=TargetReturnValue(
                            minimum_percent_pa=minimum,
                            maximum_percent_pa=maximum,
                        ),
                        raw_value=window.quote,
                        quote=window.quote,
                        page_number=window.page_number,
                        document=window.document,
                        score=score,
                    )
                )

            continue

        exact_match = TARGET_RETURN_EXACT_PATTERN.search(window.normalized)

        if exact_match is None:
            continue

        value = _parse_number(exact_match.group("value"))

        score = (
            _document_priority(
                window.document,
                TARGET_DOCUMENT_PRIORITY,
            )
            + 70
        )

        candidates.append(
            Candidate(
                value=TargetReturnValue(value_percent_pa=value),
                raw_value=window.quote,
                quote=window.quote,
                page_number=window.page_number,
                document=window.document,
                score=score,
            )
        )

    return _candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No explicitly stated target or expected annual return "
            "was found. Historical performance was not used "
            "as a substitute."
        ),
    )


def extract_fees(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[FeeCollection]:
    candidates: list[Candidate[FeeCollection]] = []

    for extraction_document in documents:
        for page in extraction_document.document.pages:
            fee_items: dict[
                FeeType,
                FeeItem,
            ] = {}

            fee_lines: list[str] = []

            for raw_line in page.text.splitlines():
                line = raw_line.strip()

                if not line:
                    continue

                normalized = normalize_search_text(line)

                for fee_type, keywords in FEE_KEYWORDS:
                    if not any(keyword in normalized for keyword in keywords):
                        continue

                    fee_item = _parse_fee_line(
                        fee_type=fee_type,
                        line=line,
                        normalized=normalized,
                    )

                    if fee_item is None:
                        continue

                    fee_items[fee_type] = fee_item

                    if line not in fee_lines:
                        fee_lines.append(line)

            if not fee_items:
                continue

            quote = "\n".join(fee_lines)

            score = (
                _document_priority(
                    extraction_document,
                    FEE_DOCUMENT_PRIORITY,
                )
                + 60
                + min(
                    len(fee_items) * 10,
                    40,
                )
            )

            candidates.append(
                Candidate(
                    value=FeeCollection(items=list(fee_items.values())),
                    raw_value=quote,
                    quote=quote,
                    page_number=page.page_number,
                    document=extraction_document,
                    score=score,
                )
            )

    return _candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No quantified entry, management, performance, exit "
            "or ongoing fee was found in the parsed public sources."
        ),
    )


def extract_aum(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[AssetsUnderManagementValue]:
    candidates: list[Candidate[AssetsUnderManagementValue]] = []

    aum_keywords = (
        "majetek fondu",
        "hodnota majetku",
        "cista aktiva",
        "fondovy kapital",
        "net assets",
        "fund assets",
        "net asset value",
        "assets under management",
    )

    manager_keywords = (
        "investicni spolecnost spravuje",
        "spravcovska spolecnost spravuje",
        "manager manages",
        "company manages",
    )

    for window in _iter_windows(documents):
        if not any(keyword in window.normalized for keyword in aum_keywords):
            continue

        if any(keyword in window.normalized for keyword in manager_keywords):
            continue

        money_match = MONEY_PATTERN.search(window.normalized)

        if money_match is None:
            continue

        as_of = _extract_date(window.normalized)

        if as_of is None:
            continue

        amount = _parse_number(money_match.group("amount"))

        multiplier = _money_multiplier(money_match.group("multiplier"))

        currency = _normalize_currency(money_match.group("currency"))

        metric_type = _detect_aum_metric(window.normalized)

        score = (
            _document_priority(
                window.document,
                AUM_DOCUMENT_PRIORITY,
            )
            + 80
        )

        candidates.append(
            Candidate(
                value=AssetsUnderManagementValue(
                    amount=amount * multiplier,
                    currency=currency,
                    metric_type=metric_type,
                    as_of=as_of,
                ),
                raw_value=window.quote,
                quote=window.quote,
                page_number=window.page_number,
                document=window.document,
                score=score,
            )
        )

    return _candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No dated and quantified fund-level assets-under-management "
            "or net-assets value was found."
        ),
    )


def _parse_fee_line(
    *,
    fee_type: FeeType,
    line: str,
    normalized: str,
) -> FeeItem | None:
    maximum = any(
        keyword in normalized
        for keyword in (
            "maximalni",
            "nejvyse",
            "az ",
            "up to",
            "maximum",
        )
    )

    range_match = PERCENT_RANGE_PATTERN.search(normalized)

    if range_match is not None:
        maximum_value = _parse_number(range_match.group("maximum"))

        return FeeItem(
            type=fee_type,
            rate_percent=maximum_value,
            frequency=_fee_frequency(fee_type),
            maximum=True,
            basis=line,
            condition=line,
        )

    percent_match = PERCENT_PATTERN.search(normalized)

    if percent_match is not None:
        return FeeItem(
            type=fee_type,
            rate_percent=_parse_number(percent_match.group("value")),
            frequency=_fee_frequency(fee_type),
            maximum=maximum,
            basis=line,
        )

    if any(
        keyword in normalized
        for keyword in (
            "bez poplatku",
            "bez vstupniho poplatku",
            "no fee",
            "free of charge",
        )
    ):
        return FeeItem(
            type=fee_type,
            rate_percent=0,
            frequency=_fee_frequency(fee_type),
            maximum=False,
            basis=line,
        )

    money_match = MONEY_PATTERN.search(normalized)

    if money_match is None:
        return None

    return FeeItem(
        type=fee_type,
        fixed_amount=(
            _parse_number(money_match.group("amount"))
            * _money_multiplier(money_match.group("multiplier"))
        ),
        currency=_normalize_currency(money_match.group("currency")),
        frequency=_fee_frequency(fee_type),
        maximum=maximum,
        basis=line,
    )


def _fee_frequency(
    fee_type: FeeType,
) -> FeeFrequency:
    if fee_type in {
        FeeType.ENTRY,
        FeeType.EXIT,
    }:
        return FeeFrequency.ONE_OFF

    if fee_type is FeeType.PERFORMANCE:
        return FeeFrequency.CONDITIONAL

    return FeeFrequency.ANNUAL


def _detect_aum_metric(
    normalized: str,
) -> AumMetricType:
    if "net asset value" in normalized or " nav " in f" {normalized} ":
        return AumMetricType.NAV

    if "cista aktiva" in normalized or "net assets" in normalized:
        return AumMetricType.NET_ASSETS

    if "fondovy kapital" in normalized:
        return AumMetricType.EQUITY

    if "aktiva" in normalized or "fund assets" in normalized:
        return AumMetricType.ASSETS_TOTAL

    return AumMetricType.FUND_AUM


def _extract_date(
    normalized: str,
) -> date | None:
    iso_match = DATE_ISO_PATTERN.search(normalized)

    if iso_match is not None:
        return _safe_date(
            year=int(iso_match.group("year")),
            month=int(iso_match.group("month")),
            day=int(iso_match.group("day")),
        )

    numeric_match = DATE_NUMERIC_PATTERN.search(normalized)

    if numeric_match is not None:
        return _safe_date(
            year=int(numeric_match.group("year")),
            month=int(numeric_match.group("month")),
            day=int(numeric_match.group("day")),
        )

    text_match = DATE_TEXT_PATTERN.search(normalized)

    if text_match is None:
        return None

    month_name = text_match.group("month")

    month = MONTHS.get(month_name)

    if month is None:
        return None

    return _safe_date(
        year=int(text_match.group("year")),
        month=month,
        day=int(text_match.group("day")),
    )


def _safe_date(
    *,
    year: int,
    month: int,
    day: int,
) -> date | None:
    try:
        return date(
            year,
            month,
            day,
        )
    except ValueError:
        return None


def _money_multiplier(
    raw_multiplier: str | None,
) -> float:
    if raw_multiplier is None:
        return 1

    normalized = normalize_search_text(raw_multiplier)

    if normalized in {
        "tis",
        "tisic",
    }:
        return 1_000

    if normalized in {
        "mil",
        "milion",
        "milionu",
        "million",
    }:
        return 1_000_000

    if normalized in {
        "mld",
        "miliarda",
        "miliard",
        "billion",
    }:
        return 1_000_000_000

    return 1


def _parse_number(
    raw_value: str,
) -> float:
    value = raw_value.replace("\u00a0", "").replace(" ", "").strip()

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(
                ".",
                "",
            ).replace(
                ",",
                ".",
            )
        else:
            value = value.replace(
                ",",
                "",
            )

    elif "," in value:
        parts = value.split(",")

        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3):
            value = "".join(parts)
        else:
            value = value.replace(
                ",",
                ".",
            )

    elif "." in value:
        parts = value.split(".")

        if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3):
            value = "".join(parts)

    return float(value)


def _normalize_currency(
    raw_currency: str,
) -> str:
    normalized = normalize_search_text(raw_currency)

    if normalized in {
        "kc",
        "czk",
    }:
        return "CZK"

    if normalized == "eur":
        return "EUR"

    if normalized == "usd":
        return "USD"

    raise ValueError(f"Unsupported currency: {raw_currency}")


def _iter_windows(
    documents: list[ExtractionDocument],
) -> list[TextWindow]:
    windows: list[TextWindow] = []

    for extraction_document in documents:
        for page in extraction_document.document.pages:
            lines = [line.strip() for line in page.text.splitlines() if line.strip()]

            for index in range(len(lines)):
                start = max(
                    0,
                    index - 1,
                )

                end = min(
                    len(lines),
                    index + 2,
                )

                quote = "\n".join(lines[start:end])

                windows.append(
                    TextWindow(
                        document=extraction_document,
                        page_number=page.page_number,
                        quote=quote,
                        normalized=normalize_search_text(quote),
                    )
                )

    return windows


def _document_priority(
    document: ExtractionDocument,
    priorities: dict[
        DocumentType,
        int,
    ],
) -> int:
    document_type = _document_type(document.record.document_type)

    return priorities.get(
        document_type,
        10,
    )


def _document_type(
    raw_document_type: str | None,
) -> DocumentType:
    if raw_document_type is None:
        return DocumentType.OTHER

    try:
        return DocumentType(raw_document_type)
    except ValueError:
        return DocumentType.OTHER


def _candidate_or_missing[ValueT](
    *,
    fund_name: str,
    candidates: list[Candidate[ValueT]],
    documents: list[ExtractionDocument],
    missing_detail: str,
) -> FieldResult[ValueT]:
    if not candidates:
        return _missing_result(
            documents=documents,
            detail=missing_detail,
        )

    best_candidate = max(
        candidates,
        key=lambda candidate: (
            candidate.score,
            candidate.document.record.source_id,
        ),
    )

    confidence = _confidence_from_score(best_candidate.score)

    source_record = best_candidate.document.record

    return FieldResult[ValueT](
        status=FieldStatus.FOUND,
        value=best_candidate.value,
        raw_value=best_candidate.raw_value,
        scope=DataScope(
            type=ScopeType.FUND,
            fund_name=fund_name,
        ),
        source=Evidence(
            source=SourceMetadata(
                url=HttpUrl(source_record.url),
                document_type=_document_type(source_record.document_type),
                retrieved_at=_source_datetime(source_record),
                title=source_record.title,
            ),
            quote=best_candidate.quote,
            page=best_candidate.page_number,
        ),
        extraction=ExtractionMetadata(
            method=ExtractionMethod.REGEX,
            confidence=confidence,
            review_required=(confidence is Confidence.LOW),
        ),
    )


def _missing_result[ValueT](
    *,
    documents: list[ExtractionDocument],
    detail: str,
) -> FieldResult[ValueT]:
    reason_code = ReasonCode.NOT_QUANTIFIED if documents else ReasonCode.SOURCE_NOT_FOUND

    return FieldResult[ValueT](
        status=FieldStatus.NOT_FOUND,
        reason=MissingReason(
            code=reason_code,
            detail=detail,
        ),
        attempted_sources=[
            SourceAttempt(
                url=HttpUrl(document.record.url),
                retrieved_at=_source_datetime(document.record),
                outcome=reason_code,
                document_type=_document_type(document.record.document_type),
                detail=detail,
            )
            for document in documents
        ],
    )


def _source_datetime(
    record: ParsedDocumentRecord,
) -> datetime:
    raw_value = record.retrieved_at or record.parsed_at

    parsed = datetime.fromisoformat(raw_value)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed


def _confidence_from_score(
    score: int,
) -> Confidence:
    if score >= 145:
        return Confidence.HIGH

    if score >= 105:
        return Confidence.MEDIUM

    return Confidence.LOW
