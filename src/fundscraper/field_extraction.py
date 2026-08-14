from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from pydantic import HttpUrl

from fundscraper.anydoc_parser import is_anydoc_parser
from fundscraper.conflict_resolution import (
    UNKNOWN_DATE,
    CandidateFacts,
    ConflictLedger,
    ConflictOutcome,
    ConflictRecord,
    Resolution,
    classify_authority,
    money_key,
    number_key,
    resolve_group,
)
from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_dates import extract_document_dates
from fundscraper.document_parser import ParsedDocument
from fundscraper.extended_validation import fallback_extraction_metadata
from fundscraper.field_definitions import (
    FUND_CAPITAL_READING_LABELS,
    HOLDING_PERIOD_FROM_PATTERN,
    HOLDING_PERIOD_RANGE_PATTERN,
    HOLDING_PERIOD_TO_PATTERN,
    classify_annualization,
    classify_capital_metric,
    classify_horizon_kind,
    classify_return_type,
    is_negotiated_fee,
    label_is_negated,
    months_from_period,
    refers_to_period_end,
    share_class_code,
    states_benchmark_linked_return,
    states_no_published_return,
)
from fundscraper.html_discovery import normalize_search_text
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain
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
    FeeTier,
    FeeTierBasis,
    FeeType,
    FieldResult,
    FieldStatus,
    HorizonKind,
    InvestmentHorizonValue,
    MinimumInvestmentKind,
    MinimumInvestmentValue,
    MissingReason,
    ReasonCode,
    ReturnType,
    ScopeType,
    SourceAttempt,
    SourceMetadata,
    TargetReturnValue,
)
from fundscraper.table_extraction import iter_labelled_rows, leading_percentage

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
    # A fund website states the subscription minimum in words -
    # "1 mil. Kč", "3,5 mil. Kč" - as often as it writes it out. The
    # group is named as the money pattern names it, so the same reader
    # applies the scale.
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


TARGET_RETURN_RANGE_PATTERN = re.compile(
    rf"""
    (?:
        cilov[ay]
        |
        cilen[ay]
        |
        ocekavan[ay]
        |
        predpokladan[ay]
        |
        prednostn[ei]
        |
        prioritn[ei]
        |
        preferencn[ei]
        |
        garantovan[ay]
        |
        zarucen[ay]
        |
        minimaln[ei]
        |
        target
        |
        expected
        |
        anticipated
        |
        preferred
        |
        priority
        |
        guaranteed
        |
        minimum
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
        cilen[ay]
        |
        ocekavan[ay]
        |
        predpokladan[ay]
        |
        prednostn[ei]
        |
        prioritn[ei]
        |
        preferencn[ei]
        |
        garantovan[ay]
        |
        zarucen[ay]
        |
        minimaln[ei]
        |
        target
        |
        expected
        |
        anticipated
        |
        preferred
        |
        priority
        |
        guaranteed
        |
        minimum
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


# A hurdle rate is written without any return word next to it, as in
# "20 % ze zisku nad hurdle rate 7 % rocne". The rate that follows the
# wording is the hurdle; the percentage in front of it is the fee.
HURDLE_RATE_PATTERN = re.compile(
    rf"""
    hurdle
    (?:\s+rate)?
    \D{{0,40}}?
    (?P<value>{NUMBER_PATTERN})
    \s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)


# A Czech fee range writes the unit after both bounds, for example
# "od 0 % do 6 %". Reading only the first number stores the lower bound
# of a range as if it were the fee.
PERCENT_RANGE_WITH_UNITS_PATTERN = re.compile(
    rf"""
    (?P<minimum>{NUMBER_PATTERN})
    \s*%
    \s*
    (?:
        -
        |
        az
        |
        do
        |
        to
    )
    \s*
    (?P<maximum>{NUMBER_PATTERN})
    \s*%
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Wording marking a fee as an upper limit. Czech fee tables abbreviate it
# to "max.", which is why a documented cap was stored as a fixed rate.
FEE_MAXIMUM_MARKERS: Final[tuple[str, ...]] = (
    "maximalni",
    "maximaln",
    "maximum",
    "max.",
    "max ",
    "nejvyse",
    "az ",
    "up to",
)


# How far after its label the amount of a fund asset value may stand. A
# financial statement writes "Fondovy kapital: 693 601 745 Kc", while an
# unrelated amount in a neighbouring sentence is much further away.
AUM_LABEL_MAXIMUM_DISTANCE: Final = 60


# A four digit number in the calendar range is a year. It is never a
# percentage, however close to a percent sign a broken PDF layout puts it.
YEAR_LIKE_PATTERN: Final = re.compile(r"^(?:19|20)\d{2}$")


# Wording showing a percentage describes what the fund already earned,
# not what it targets.
PAST_PERFORMANCE_KEYWORDS: Final[tuple[str, ...]] = (
    "vykonnost fondu",
    "za poslednich",
    "od zalozeni",
    "historicka vykonnost",
    "minula vykonnost",
    "dosazene zhodnoceni",
    "past performance",
)


# The performance scenarios of a KID are regulatory projections under
# assumed conditions, not a target return of the fund.
PERFORMANCE_SCENARIO_KEYWORDS: Final[tuple[str, ...]] = (
    "scenar",
    "stresovy",
    "neprizniv",
    "umerny",
    "prizniv",
    "performance scenario",
    "stress scenario",
    "unfavourable",
    "moderate scenario",
    "favourable",
)


# Wording showing an amount is the price or value of one investment
# share rather than the minimum an investor has to subscribe.
SHARE_PRICE_KEYWORDS: Final[tuple[str, ...]] = (
    "aktualni hodnota",
    "hodnota investicni akcie",
    "hodnota jedne investicni akcie",
    "jmenovita hodnota",
    "cena investicni akcie",
    "kurz",
    "net asset value",
    "nav na akcii",
)


# A unit declared for a whole table or statement, such as the header
# "(v tis. Kc)" of a Czech financial statement.
DECLARED_UNIT_MULTIPLIERS: Final[tuple[tuple[str, float], ...]] = (
    ("v celych tis", 1_000.0),
    ("v tisicich", 1_000.0),
    ("v tis", 1_000.0),
    ("tis. kc", 1_000.0),
    ("v milionech", 1_000_000.0),
    ("v mil", 1_000_000.0),
    ("v miliardach", 1_000_000_000.0),
    ("v mld", 1_000_000_000.0),
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
    # An amount never continues a longer number. Without this guard the
    # tail of a date such as "31.12.2020" is read as the amount 12.2020.
    (?<![\d.,])
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

    # Offsets inside the normalized document text, used to tell which
    # fund section of a multi-fund page a value belongs to.
    document_offset: int = 0
    anchor_offset: int = 0


@dataclass(frozen=True, slots=True)
class Candidate[ValueT]:
    value: ValueT
    raw_value: str
    quote: str
    page_number: int | None
    document: ExtractionDocument
    score: int

    # Offset of the matched value inside the normalized document text.
    value_offset: int = 0


class SourceScope(StrEnum):
    """
    Which entity a source document actually describes.

    A shared manager or administrator website hosts the documents of many
    funds. Accepting a value from such a document without confirming the
    entity attributes one fund's data to another, so every scope other
    than the requested fund itself must be rejected.
    """

    EXACT_FUND = "exact_fund"
    SUBFUND = "subfund"
    SHARE_CLASS = "share_class"
    MANAGER = "manager"
    OTHER_FUND = "other_fund"
    GENERIC = "generic"


# Scopes whose values may be attributed to the requested fund.
ACCEPTED_SCOPES: Final[frozenset[SourceScope]] = frozenset(
    {
        SourceScope.EXACT_FUND,
        SourceScope.SUBFUND,
        SourceScope.SHARE_CLASS,
    }
)


# The legal form that closes the name of a Czech fund. The words right in
# front of it are the name itself, which is what separates one fund of a
# manager from its siblings, such as CREDITAS ASSETS from CREDITAS fond.
FUND_ENTITY_KEYWORD_PATTERN: Final = re.compile(
    r"\b(?:sicav|investicni\s+fond|podfond)\b",
)


# How many words in front of the legal form are inspected as the name.
FUND_NAME_WINDOW_WORDS: Final = 8


SUBFUND_KEYWORDS: Final[tuple[str, ...]] = (
    "podfond",
    "subfund",
    "sub-fund",
)


SHARE_CLASS_KEYWORDS: Final[tuple[str, ...]] = (
    "investicni akcie tridy",
    "trida investicnich akcii",
    "share class",
    "tridy pia",
    "tride pia",
)


# How much of the document is inspected for its entity identity. The
# legal name of a fund appears on its title page.
IDENTITY_TEXT_CHARACTERS: Final = 5_000


SCOPE_MISMATCH_KEYWORDS: Final[tuple[str, ...]] = (
    "skupina spravuje",
    "investicni skupina spravuje",
    "investicni spolecnost spravuje",
    "obhospodarovatel spravuje",
    "spravce spravuje",
    "aktiva ve sprave skupiny",
    "majetek ve sprave skupiny",
    "celkovy objem aktiv ve sprave",
    "vsechny fondy",
    "napric fondy",
    "manager manages",
    "company manages",
    "group manages",
    "assets under management of the group",
    "total assets under management",
)


FUND_NAME_NOISE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "as",
        "s",
        "sicav",
        "fond",
        "fund",
        "fonds",
        "investicni",
        "investment",
        "spolecnost",
        "podfond",
        "subfund",
        "otevreny",
        "uzavreny",
        "promennym",
        # "s proměnným základním kapitálem" is the legal form of a SICAV,
        # written out in the registered name of 29 of the canonical funds.
        # None of its three words tells one fund from another, and a
        # mention is only read as far as its "investiční fond" head, so a
        # token standing behind that head can never appear in one. Left
        # in, it made those funds unable to match their own legal name -
        # on their own homepage the name then read as a foreign fund and
        # every value on the page was refused.
        "zakladnim",
        "kapitalem",
    }
)


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
    fund_web: str | None = None,
    ledger: ConflictLedger | None = None,
) -> ExtractedFundFields:
    """Extract all supported fund fields from parsed documents."""

    return ExtractedFundFields(
        investment_horizon=extract_investment_horizon(
            fund_name=fund_name,
            documents=documents,
            fund_web=fund_web,
            ledger=ledger,
        ),
        minimum_investment=extract_minimum_investment(
            fund_name=fund_name,
            documents=documents,
            fund_web=fund_web,
            ledger=ledger,
        ),
        target_return=extract_target_return(
            fund_name=fund_name,
            documents=documents,
            fund_web=fund_web,
            ledger=ledger,
        ),
        fees=extract_fees(
            fund_name=fund_name,
            documents=documents,
            fund_web=fund_web,
            ledger=ledger,
        ),
        assets_under_management=extract_aum(
            fund_name=fund_name,
            documents=documents,
            fund_web=fund_web,
            ledger=ledger,
        ),
    )


def extract_investment_horizon(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
    fund_web: str | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldResult[InvestmentHorizonValue]:
    candidates: list[Candidate[InvestmentHorizonValue]] = []

    for window in _iter_windows(documents):
        match = HORIZON_PATTERN.search(window.normalized)

        if match is None:
            continue

        years = parse_number(match.group("years"))

        if not 0 < years <= 100:
            continue

        score = (
            document_priority(
                window.document,
                HORIZON_DOCUMENT_PRIORITY,
            )
            + 70
        )

        if "doporucen" in window.normalized:
            score += 15

        # The words immediately around the number decide whether it is
        # the horizon or the least of it. Read from the match rather than
        # the whole window, because a page saying "min. 100 000 Kc"
        # elsewhere must not turn a five-year horizon into a floor.
        kind = classify_horizon_kind(
            window.normalized[max(match.start() - 40, 0) : match.end() + 20]
        )

        candidates.append(
            Candidate(
                value=InvestmentHorizonValue(
                    recommended_years=years,
                    kind=kind,
                    minimum_years=(years if kind is HorizonKind.MINIMUM else None),
                ),
                raw_value=window.quote,
                quote=window.quote,
                page_number=window.page_number,
                document=window.document,
                score=score,
                value_offset=window.document_offset + match.start(),
            )
        )

    return candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No quantified recommended investment horizon was found in the parsed public sources."
        ),
        detect_conflicts=True,
        value_key=lambda value: number_key(value.recommended_years),
        field="investment_horizon",
        fund_web=fund_web,
        priorities=HORIZON_DOCUMENT_PRIORITY,
        ledger=ledger,
    )


def extract_minimum_investment(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
    fund_web: str | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldResult[MinimumInvestmentValue]:
    candidates: list[Candidate[MinimumInvestmentValue]] = []

    for window in _iter_windows(documents):
        match = MINIMUM_INVESTMENT_PATTERN.search(window.normalized)

        if match is None:
            continue

        if any(keyword in window.normalized for keyword in SHARE_PRICE_KEYWORDS):
            # The number is the price or value of one investment share.
            continue

        # The scale word is applied before the fraction guard below:
        # "3,5 mil. Kč" is a whole number of crowns, and refusing it as a
        # fraction would lose a minimum the site states plainly.
        amount = parse_money_amount(match)

        currency = normalize_currency(match.group("currency"))

        if amount < 0:
            continue

        if amount != int(amount):
            # A subscription minimum is never a fraction of a unit.
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
            document_priority(
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
                value_offset=window.document_offset + match.start(),
            )
        )

    return candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=("No quantified minimum investment was found in the parsed public sources."),
        detect_conflicts=True,
        value_key=lambda value: money_key(
            amount=value.amount,
            currency=value.currency,
        ),
        # A subscription minimum of one share class is not a different
        # answer from the minimum of another, so only candidates about
        # the same class and kind contest one another.
        conflict_key=lambda value: (
            value.kind.value,
            value.share_class or "",
        ),
        field="minimum_investment",
        fund_web=fund_web,
        priorities=MINIMUM_DOCUMENT_PRIORITY,
        ledger=ledger,
    )


def extract_target_return(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
    fund_web: str | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldResult[TargetReturnValue]:
    candidates: list[Candidate[TargetReturnValue]] = []

    not_published: TextWindow | None = None

    for window in _iter_windows(documents):
        if states_no_published_return(window.normalized) and not_published is None:
            not_published = window

        if _describes_other_than_a_target(window.normalized):
            continue

        candidate = _target_return_candidate(window)

        if candidate is not None:
            candidates.append(candidate)

    if not candidates and not_published is not None:
        # The source answers the question: the fund publishes no target.
        # That is a different result from having found nothing, and the
        # reason code has to say so.
        return _not_published_result(window=not_published)

    return candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No explicitly stated target or expected annual return "
            "was found. Historical performance was not used "
            "as a substitute."
        ),
        detect_conflicts=True,
        value_key=lambda value: (
            number_key(value.value_percent_pa),
            number_key(value.minimum_percent_pa),
            number_key(value.maximum_percent_pa),
        ),
        # An expected return, a hurdle and a guaranteed minimum are
        # different concepts of the same fund and may all be true, so
        # only two statements of the same concept for the same class
        # contradict each other.
        conflict_key=lambda value: (
            value.return_type.value,
            value.share_class or "",
            value.subfund or "",
        ),
        field="target_return",
        fund_web=fund_web,
        priorities=TARGET_DOCUMENT_PRIORITY,
        ledger=ledger,
    )


def _target_return_candidate(
    window: TextWindow,
) -> Candidate[TargetReturnValue] | None:
    """
    Build the return one window states, with the concept it describes.

    An expected return, a guaranteed minimum, a preferred return of a
    priority share class and a hurdle rate are four different promises.
    Storing them all as "target" told an investor that a fund aims at a
    number it in fact only pays before its founder is paid.
    """

    # "prednostne do rustu PIA az do vyse jejich zhodnoceni 2TR + 1 %
    # p.a." states a rate that moves with a reference rate. The stored
    # model has nowhere to put the reference, and a delivered 1 % a year
    # is not a weaker version of the truth but a different claim, so the
    # window yields nothing rather than its spread.
    if states_benchmark_linked_return(window.normalized):
        return None

    return_type = classify_return_type(window.normalized)

    annualization = classify_annualization(window.normalized)

    share_class = share_class_code(window.quote)

    range_match = TARGET_RETURN_RANGE_PATTERN.search(window.normalized)

    if range_match is not None:
        if _is_year_like(range_match.group("minimum")) or _is_year_like(
            range_match.group("maximum")
        ):
            return None

        minimum = parse_number(range_match.group("minimum"))

        maximum = parse_number(range_match.group("maximum"))

        if minimum > maximum:
            return None

        return _build_target_candidate(
            window=window,
            value=TargetReturnValue(
                minimum_percent_pa=minimum,
                maximum_percent_pa=maximum,
                return_type=(return_type or ReturnType.RANGE),
                annualization=annualization,
                share_class=share_class,
            ),
            offset=range_match.start(),
            bonus=80,
            classified=return_type is not None,
        )

    hurdle_match = HURDLE_RATE_PATTERN.search(window.normalized)

    if hurdle_match is not None and not _is_year_like(hurdle_match.group("value")):
        return _build_target_candidate(
            window=window,
            value=TargetReturnValue(
                value_percent_pa=parse_number(hurdle_match.group("value")),
                return_type=ReturnType.HURDLE,
                annualization=annualization,
                share_class=share_class,
            ),
            offset=hurdle_match.start(),
            bonus=75,
            classified=True,
        )

    exact_match = TARGET_RETURN_EXACT_PATTERN.search(window.normalized)

    if exact_match is None or _is_year_like(exact_match.group("value")):
        return None

    return _build_target_candidate(
        window=window,
        value=TargetReturnValue(
            value_percent_pa=parse_number(exact_match.group("value")),
            return_type=(return_type or ReturnType.TARGET),
            annualization=annualization,
            share_class=share_class,
        ),
        offset=exact_match.start(),
        bonus=70,
        classified=return_type is not None,
    )


def _build_target_candidate(
    *,
    window: TextWindow,
    value: TargetReturnValue,
    offset: int,
    bonus: int,
    classified: bool,
) -> Candidate[TargetReturnValue]:
    return Candidate(
        value=value,
        raw_value=window.quote,
        quote=window.quote,
        page_number=window.page_number,
        document=window.document,
        score=(
            document_priority(
                window.document,
                TARGET_DOCUMENT_PRIORITY,
            )
            + bonus
            + (10 if classified else 0)
        ),
        value_offset=window.document_offset + offset,
    )


def _not_published_result(
    *,
    window: TextWindow,
) -> FieldResult[TargetReturnValue]:
    record = window.document.record

    return FieldResult[TargetReturnValue](
        status=FieldStatus.NOT_FOUND,
        reason=MissingReason(
            code=ReasonCode.NOT_PUBLICLY_DISCLOSED,
            detail=(
                "The source states that no target return is published: "
                f"{window.quote.strip()[:300]}"
            ),
        ),
        attempted_sources=[
            SourceAttempt(
                url=HttpUrl(record.url),
                retrieved_at=source_datetime(record),
                outcome=ReasonCode.NOT_PUBLICLY_DISCLOSED,
                document_type=resolve_document_type(record.document_type),
                detail=window.quote.strip()[:300],
            )
        ],
    )


def extract_fees(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
    fund_web: str | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldResult[FeeCollection]:
    candidates: list[Candidate[FeeCollection]] = []

    for extraction_document in documents:
        normalized_document, document_lines = normalized_document_lines(extraction_document)

        # A fee table states the rate in the same row as the fee it
        # belongs to. Read as a grid the pairing is certain; read as text
        # it is a guess about which percentage stood nearest.
        candidates.extend(
            _fee_candidates_from_tables(
                document=extraction_document,
                normalized_document=normalized_document,
            )
        )

        boundaries = section_boundaries(normalized_document)

        # Fees are collected per fund section, so a manager page listing
        # several funds cannot merge their fee tables into one value.
        groups: dict[
            tuple[int | None, int],
            tuple[dict[FeeType, FeeItem], list[str], int],
        ] = {}

        for line, page_number, offset in document_lines:
            normalized = normalize_search_text(line)

            line_items: dict[FeeType, FeeItem] = {}

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

                line_items[fee_type] = fee_item

            if is_collapsed_fee_line(line_items):
                continue

            for fee_type, fee_item in line_items.items():
                key = (
                    page_number,
                    section_index(
                        boundaries=boundaries,
                        offset=offset,
                    ),
                )

                fee_items, fee_lines, _ = groups.setdefault(
                    key,
                    (
                        {},
                        [],
                        offset,
                    ),
                )

                fee_items[fee_type] = fee_item

                if line not in fee_lines:
                    fee_lines.append(line)

        for (page_number, _), (
            fee_items,
            fee_lines,
            group_offset,
        ) in groups.items():
            if not fee_items:
                continue

            quote = "\n".join(fee_lines)

            score = (
                document_priority(
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
                    page_number=page_number,
                    document=extraction_document,
                    score=score,
                    value_offset=group_offset,
                )
            )

    return candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No quantified entry, management, performance, exit "
            "or ongoing fee was found in the parsed public sources."
        ),
        detect_conflicts=True,
        # Two fee schedules disagree when they state a different rate for
        # a fee an investor pays. One document listing more fee types
        # than another says more, not something else, so the comparison
        # is made over the rate of each type and its tiers.
        value_key=fee_collection_key,
        display=describe_fee_collection,
        field="fees",
        fund_web=fund_web,
        priorities=FEE_DOCUMENT_PRIORITY,
        ledger=ledger,
    )


def is_collapsed_fee_line(
    line_items: dict[FeeType, FeeItem],
) -> bool:
    """
    Return whether one line handed a single rate to several kinds of fee.

    A converter that merges a whole cost table into one row leaves every
    fee label and one percentage on the same line. Read as text that line
    looks like an entry fee, an exit fee and a performance fee all
    charged at the rate that happened to come first, which is a wrong
    value rather than a missing one. Rates that are all zero are left
    alone: a table stating that nothing is charged really does repeat
    itself.
    """

    if len(line_items) < 2:
        return False

    rates = [item.rate_percent for item in line_items.values()]

    if any(rate is None for rate in rates):
        return False

    first = rates[0]

    if not first:
        return False

    return all(rate == first for rate in rates)


# What a fee read from a table row is worth above the same fee read from
# a line of flattened text.
FEE_TABLE_SCORE_BONUS: Final = 30


def _fee_candidates_from_tables(
    *,
    document: ExtractionDocument,
    normalized_document: str,
) -> list[Candidate[FeeCollection]]:
    """Read the fees stated by the rows of the tables of one document."""

    candidates: list[Candidate[FeeCollection]] = []

    for page_number, table in document.document.iter_tables():
        items: dict[FeeType, FeeItem] = {}

        quotes: list[str] = []

        for row in iter_labelled_rows(table):
            rate = next(
                (
                    found
                    for cell in row.cells
                    for found in [leading_percentage(cell)]
                    if found is not None
                ),
                None,
            )

            if rate is None:
                continue

            normalized_label = normalize_search_text(row.label)

            fee_type = next(
                (
                    candidate_type
                    for candidate_type, keywords in FEE_KEYWORDS
                    if any(keyword in normalized_label for keyword in keywords)
                ),
                None,
            )

            if fee_type is None or fee_type in items:
                continue

            if rate < 0 or rate > FEE_MAXIMUM_RATE_PERCENT:
                continue

            normalized_row = normalize_search_text(row.row_text)

            items[fee_type] = FeeItem(
                type=fee_type,
                rate_percent=rate,
                frequency=_fee_frequency(fee_type),
                maximum=any(marker in normalized_row for marker in FEE_MAXIMUM_MARKERS),
                basis=row.row_text,
                condition=row.row_text,
                tiers=_parse_fee_tiers(
                    line=row.row_text,
                    normalized=normalized_row,
                ),
            )

            quotes.append(row.row_text)

        if not items:
            continue

        quote = "\n".join(quotes)

        needle = normalize_search_text(quote)[:60]

        candidates.append(
            Candidate(
                value=FeeCollection(items=list(items.values())),
                raw_value=quote,
                quote=quote,
                page_number=page_number,
                document=document,
                score=(
                    document_priority(
                        document,
                        FEE_DOCUMENT_PRIORITY,
                    )
                    + 60
                    + FEE_TABLE_SCORE_BONUS
                    + min(
                        len(items) * 10,
                        40,
                    )
                ),
                value_offset=max(normalized_document.find(needle), 0),
            )
        )

    return candidates


# A fee above this is a parsing artefact rather than a rate an investor
# pays. The delivered audit uses the same bound.
FEE_MAXIMUM_RATE_PERCENT: Final = 100.0


def extract_aum(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
    fund_web: str | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldResult[AssetsUnderManagementValue]:
    candidates: list[Candidate[AssetsUnderManagementValue]] = []

    # The shared fund-level vocabulary, not a second private list. It
    # carries the inflected forms a Czech report actually uses.
    aum_keywords = FUND_CAPITAL_READING_LABELS

    manager_keywords = (
        "investicni spolecnost spravuje",
        "spravcovska spolecnost spravuje",
        "manager manages",
        "company manages",
    )

    for window in _iter_windows(documents):
        if any(keyword in window.normalized for keyword in manager_keywords):
            continue

        money_match = _money_after_label(
            normalized=window.normalized,
            labels=aum_keywords,
        )

        if money_match is None:
            continue

        as_of = extract_date(window.normalized)

        if as_of is None:
            as_of = _period_end_date(window)

        if as_of is None:
            continue

        amount = parse_money_amount(money_match)

        if money_match.group("multiplier") is None:
            # A financial statement declares its unit once, in a table
            # header such as "(v tis. Kc)", not next to every number.
            amount *= declared_multiplier(window.normalized) or 1

        currency = normalize_currency(money_match.group("currency"))

        metric_type = _detect_aum_metric(window.normalized)

        recency_score = min(
            max(
                as_of.year - 2000,
                0,
            ),
            50,
        )

        score = (
            document_priority(
                window.document,
                AUM_DOCUMENT_PRIORITY,
            )
            + 80
            + recency_score
        )

        candidates.append(
            Candidate(
                value=AssetsUnderManagementValue(
                    amount=amount,
                    currency=currency,
                    metric_type=metric_type,
                    as_of=as_of,
                ),
                raw_value=window.quote,
                quote=window.quote,
                page_number=window.page_number,
                document=window.document,
                score=score,
                value_offset=(window.document_offset + money_match.start()),
            )
        )

    return candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            "No dated and quantified fund-level assets-under-management "
            "or net-assets value was found."
        ),
        detect_conflicts=True,
        value_key=lambda value: money_key(
            amount=value.amount,
            currency=value.currency,
        ),
        # This field is the assets of the fund *now*, so two readings of
        # the same metric compete however far apart their dates are, and
        # the ranking ladder prefers the newer one. Keeping the date in
        # the key made every date its own uncontested winner, which is
        # how a 2023 net asset value was delivered as the current assets
        # of a fund whose own history already held a 2025 one. Every
        # dated observation still survives in ``aum_history``, which
        # groups by date on purpose.
        conflict_key=lambda value: (
            value.metric_type.value,
            value.currency,
        ),
        field="assets_under_management",
        fund_web=fund_web,
        priorities=AUM_DOCUMENT_PRIORITY,
        ledger=ledger,
    )


def _parse_fee_line(
    *,
    fee_type: FeeType,
    line: str,
    normalized: str,
) -> FeeItem | None:
    """
    Read one fee row into a fee item.

    ``rate_percent`` keeps the single number the delivered output has
    always carried. Everything a row states beyond it - the tiers of a
    holding period, the class it applies to, the bounds of a negotiated
    range - is added next to it instead of replacing it.
    """

    maximum = any(keyword in normalized for keyword in FEE_MAXIMUM_MARKERS)

    tiers = _parse_fee_tiers(
        line=line,
        normalized=normalized,
    )

    negotiable = is_negotiated_fee(normalized) or None

    range_match = PERCENT_RANGE_WITH_UNITS_PATTERN.search(
        normalized
    ) or PERCENT_RANGE_PATTERN.search(normalized)

    if range_match is not None:
        if _is_year_like(range_match.group("maximum")):
            return None

        minimum_value = parse_number(range_match.group("minimum"))

        maximum_value = parse_number(range_match.group("maximum"))

        if minimum_value > maximum_value:
            return None

        return FeeItem(
            type=fee_type,
            rate_percent=maximum_value,
            frequency=_fee_frequency(fee_type),
            maximum=True,
            basis=line,
            condition=line,
            # A range collapsed to its upper bound hides that the lower
            # one exists. Both are kept, so a reader can tell a fixed
            # six per cent from "from zero to six per cent".
            minimum_rate_percent=minimum_value,
            maximum_rate_percent=maximum_value,
            tiers=tiers,
            negotiable=negotiable,
        )

    percent_match = PERCENT_PATTERN.search(normalized)

    if percent_match is not None and _is_year_like(percent_match.group("value")):
        percent_match = None

    if percent_match is not None:
        return FeeItem(
            type=fee_type,
            rate_percent=parse_number(percent_match.group("value")),
            frequency=_fee_frequency(fee_type),
            maximum=maximum,
            basis=line,
            tiers=tiers,
            negotiable=negotiable,
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
            tiers=tiers,
        )

    money_match = MONEY_PATTERN.search(normalized)

    if money_match is not None:
        return FeeItem(
            type=fee_type,
            fixed_amount=parse_money_amount(money_match),
            currency=normalize_currency(money_match.group("currency")),
            frequency=_fee_frequency(fee_type),
            maximum=maximum,
            basis=line,
            tiers=tiers,
            negotiable=negotiable,
        )

    if negotiable:
        # The fee exists but carries no published number: it is agreed
        # with the distributor. Dropping the row would report the fund as
        # charging nothing.
        return FeeItem(
            type=fee_type,
            frequency=_fee_frequency(fee_type),
            basis=line,
            details=line,
            negotiable=True,
        )

    return None


def _parse_fee_tiers(
    *,
    line: str,
    normalized: str,
) -> list[FeeTier]:
    """
    Read the conditional rows of a fee out of one line.

    "Vystupni poplatek do 1 roku - 10 %, od 1 do 2 let - 5 %, po 2 letech
    0 %" is three tiers. Reading only the first percentage reported the
    fee of an investor who leaves immediately as the fee of everyone.
    """

    percentages = [
        (
            match.start(),
            parse_number(match.group("value")),
        )
        for match in PERCENT_PATTERN.finditer(normalized)
        if not _is_year_like(match.group("value"))
    ]

    if not percentages:
        return []

    tiers: list[FeeTier] = []

    used: set[int] = set()

    clauses = _clause_spans(normalized)

    for start, from_months, to_months in _holding_periods(normalized):
        rate = _rate_of_period(
            percentages=percentages,
            start=start,
            clause=_clause_of(
                clauses=clauses,
                offset=start,
            ),
            used=used,
        )

        if rate is None:
            continue

        tiers.append(
            FeeTier(
                basis=FeeTierBasis.HOLDING_PERIOD,
                from_months=from_months,
                to_months=to_months,
                rate_percent=rate,
                condition=line,
            )
        )

    if tiers:
        return tiers

    share_class = share_class_code(line)

    if share_class is not None:
        return [
            FeeTier(
                basis=FeeTierBasis.SHARE_CLASS,
                share_class=share_class,
                rate_percent=percentages[0][1],
                condition=line,
            )
        ]

    if is_negotiated_fee(normalized):
        return [
            FeeTier(
                basis=FeeTierBasis.DISTRIBUTOR,
                rate_percent=percentages[0][1],
                condition=line,
            )
        ]

    return []


def _holding_periods(
    normalized: str,
) -> list[tuple[int, int | None, int | None]]:
    """Return every holding period a line states, with its offset."""

    periods: list[tuple[int, int | None, int | None]] = []

    for match in HOLDING_PERIOD_RANGE_PATTERN.finditer(normalized):
        unit = match.group("unit")

        periods.append(
            (
                match.end(),
                months_from_period(
                    count=int(match.group("from")),
                    unit=unit,
                ),
                months_from_period(
                    count=int(match.group("to")),
                    unit=unit,
                ),
            )
        )

    covered = {start for start, _, _ in periods}

    for match in HOLDING_PERIOD_TO_PATTERN.finditer(normalized):
        if match.end() in covered:
            continue

        periods.append(
            (
                match.end(),
                0,
                months_from_period(
                    count=int(match.group("count")),
                    unit=match.group("unit"),
                ),
            )
        )

    for match in HOLDING_PERIOD_FROM_PATTERN.finditer(normalized):
        if match.end() in covered:
            continue

        periods.append(
            (
                match.end(),
                months_from_period(
                    count=int(match.group("count")),
                    unit=match.group("unit"),
                ),
                None,
            )
        )

    return sorted(periods)


def _clause_spans(
    normalized: str,
) -> tuple[tuple[int, int], ...]:
    """
    Return the comma-separated clauses of one fee line.

    A fee schedule states one period and its rate per clause, in either
    order: "do 1 roku - 10 %, po 1 roce 0 %" and "0 % po 3 letech, 5 % do
    3 let" both do. The clause is what keeps a period from taking the
    rate of its neighbour.
    """

    spans: list[tuple[int, int]] = []

    start = 0

    for index, character in enumerate(normalized):
        if character in ",;":
            spans.append(
                (
                    start,
                    index,
                )
            )

            start = index + 1

    spans.append(
        (
            start,
            len(normalized),
        )
    )

    return tuple(spans)


def _clause_of(
    *,
    clauses: tuple[tuple[int, int], ...],
    offset: int,
) -> tuple[int, int]:
    """Return the clause an offset falls in."""

    for span in clauses:
        if span[0] <= offset <= span[1]:
            return span

    return (
        0,
        offset,
    )


def _rate_of_period(
    *,
    percentages: list[tuple[int, float]],
    start: int,
    clause: tuple[int, int],
    used: set[int],
) -> float | None:
    """
    Return the percentage that belongs to one holding period.

    Only a percentage of the same clause qualifies. Within it the one
    stated after the period is preferred, because that is how a fee table
    is usually written; a clause that puts the rate first - "0 % po 3
    letech" - is read backwards rather than reaching into the next
    clause, which published the schedule inverted.
    """

    clause_start, clause_end = clause

    available = [
        (offset, value)
        for offset, value in percentages
        if clause_start <= offset <= clause_end and offset not in used
    ]

    following = [item for item in available if item[0] >= start]

    preceding = [item for item in available if item[0] < start]

    chosen = following[0] if following else (preceding[-1] if preceding else None)

    if chosen is None:
        return None

    used.add(chosen[0])

    return chosen[1]


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


def _period_end_date(
    window: TextWindow,
) -> date | None:
    """
    Date a value that names its period instead of its day.

    Only one substitution is allowed and only when the text asks for it:
    the window has to say the figure is stated at the end of the
    accounting period, and the document has to state when that period
    ended. A publication date is never used — when a report is published
    says nothing about when its figures were measured, and a value dated
    by its own publication would be wrong by up to a year.
    """

    if not refers_to_period_end(window.normalized):
        return None

    dates = extract_document_dates(
        text=window.document.document.full_text,
        url=window.document.record.url or "",
    )

    period_end = dates.reporting_period_end

    if period_end is None:
        return None

    return period_end.value


def _detect_aum_metric(
    normalized: str,
) -> AumMetricType:
    """
    Name the capital figure a window states, using the shared labels.

    This used to carry its own short ladder, which disagreed with
    ``classify_capital_metric`` on the one label they both knew: it read
    "fondovy kapital" as equity where the shared table reads it as fund
    capital. One delivered output therefore reported 52 012 tis. Kc as
    the equity of a fund whose own history recorded the same figure, on
    the same day, as its fund capital.
    """

    metric = classify_capital_metric(normalized)

    if metric is not None:
        return metric

    if "aktiva" in normalized or "fund assets" in normalized:
        return AumMetricType.ASSETS_TOTAL

    return AumMetricType.FUND_AUM


def extract_date(
    normalized: str,
) -> date | None:
    iso_match = DATE_ISO_PATTERN.search(normalized)

    if iso_match is not None:
        return safe_date(
            year=int(iso_match.group("year")),
            month=int(iso_match.group("month")),
            day=int(iso_match.group("day")),
        )

    numeric_match = DATE_NUMERIC_PATTERN.search(normalized)

    if numeric_match is not None:
        return safe_date(
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

    return safe_date(
        year=int(text_match.group("year")),
        month=month,
        day=int(text_match.group("day")),
    )


def safe_date(
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


def _money_after_label(
    *,
    normalized: str,
    labels: tuple[str, ...],
) -> re.Match[str] | None:
    """
    Return the amount that belongs to an asset label of the text.

    The amount has to follow its label closely. Taking the first amount
    anywhere in the window attributed unrelated sums, such as a
    withholding tax stated one sentence earlier, to the fund assets.
    """

    best: re.Match[str] | None = None

    for label in labels:
        start = normalized.find(label)

        while start >= 0:
            label_end = start + len(label)

            # "z toho neinvesticni fondovy kapital: 100 000 Kc" names a
            # component of the capital, not the capital. Skipping the
            # occurrence lets the same window's "investicni fondovy
            # kapital" supply the real figure.
            if label_is_negated(
                normalized=normalized,
                label_start=start,
            ):
                start = normalized.find(label, start + 1)

                continue

            money_match = MONEY_PATTERN.search(
                normalized,
                label_end,
                label_end + AUM_LABEL_MAXIMUM_DISTANCE,
            )

            if money_match is not None and (best is None or money_match.start() < best.start()):
                best = money_match

            start = normalized.find(
                label,
                start + 1,
            )

    return best


def _is_year_like(
    raw_value: str,
) -> bool:
    """Return whether a captured number is a calendar year."""

    return YEAR_LIKE_PATTERN.match(raw_value.strip()) is not None


def _describes_other_than_a_target(
    normalized: str,
) -> bool:
    """
    Return whether a percentage in this text cannot be a target return.

    Past performance and the regulatory performance scenarios of a KID
    both state percentages next to return wording, but neither of them
    is what the fund targets.
    """

    if any(keyword in normalized for keyword in PAST_PERFORMANCE_KEYWORDS):
        return True

    return any(keyword in normalized for keyword in PERFORMANCE_SCENARIO_KEYWORDS)


def declared_multiplier(
    normalized: str,
) -> float | None:
    """Return the unit declared for the table the value belongs to."""

    for marker, multiplier in DECLARED_UNIT_MULTIPLIERS:
        if marker in normalized:
            return multiplier

    return None


def money_multiplier(
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


def parse_money_amount(
    money_match: re.Match[str],
) -> float:
    """
    Read the amount of a money match, unit included.

    A number standing in front of a unit is a decimal, never a grouped
    thousand: "1,888 mld. Kc" is 1.888 billion. Reading its comma as a
    thousands separator turned the assets of a fund into 1 888 billion.
    """

    raw_amount = money_match.group("amount")

    multiplier = money_multiplier(money_match.group("multiplier"))

    if multiplier > 1 and "," in raw_amount and "." not in raw_amount:
        decimal_value = float(
            raw_amount.replace(
                " ",
                "",
            )
            .replace(
                " ",
                "",
            )
            .replace(
                ",",
                ".",
            )
        )

        return decimal_value * multiplier

    return parse_number(raw_amount) * multiplier


def parse_number(
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


def normalize_currency(
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


def normalized_document_lines(
    document: ExtractionDocument,
) -> tuple[str, tuple[tuple[str, int | None, int], ...]]:
    """
    Return the normalized document text and the offset of every line.

    The offsets make it possible to tell which section of a multi-fund
    page a value was taken from. The text is assembled from the same
    normalized lines that the extraction windows are built from, so an
    offset inside a window is also an offset inside this text.
    """

    parts: list[str] = []

    lines: list[tuple[str, int | None, int]] = []

    offset = 0

    for page in document.document.pages:
        for raw_line in page.text.splitlines():
            line = raw_line.strip()

            if not line:
                continue

            normalized_line = normalize_search_text(line)

            if not normalized_line:
                continue

            lines.append(
                (
                    line,
                    page.page_number,
                    offset,
                )
            )

            parts.append(normalized_line)

            # Lines are joined by a single space, exactly as
            # normalize_search_text collapses the newline of a window.
            offset += len(normalized_line) + 1

    return (
        " ".join(parts),
        tuple(lines),
    )


def _iter_windows(
    documents: list[ExtractionDocument],
) -> list[TextWindow]:
    windows: list[TextWindow] = []

    for extraction_document in documents:
        _, document_lines = normalized_document_lines(extraction_document)

        for index, (_, page_number, line_offset) in enumerate(document_lines):
            start = max(
                0,
                index - 1,
            )

            end = min(
                len(document_lines),
                index + 2,
            )

            quote = "\n".join(line for line, _, _ in document_lines[start:end])

            windows.append(
                TextWindow(
                    document=extraction_document,
                    page_number=page_number,
                    quote=quote,
                    normalized=normalize_search_text(quote),
                    document_offset=document_lines[start][2],
                    anchor_offset=line_offset,
                )
            )

    return windows


def document_priority(
    document: ExtractionDocument,
    priorities: dict[
        DocumentType,
        int,
    ],
) -> int:
    document_type = resolve_document_type(document.record.document_type)

    return priorities.get(
        document_type,
        10,
    )


def resolve_document_type(
    raw_document_type: str | None,
) -> DocumentType:
    if raw_document_type is None:
        return DocumentType.OTHER

    try:
        return DocumentType(raw_document_type)
    except ValueError:
        return DocumentType.OTHER


@dataclass(frozen=True, slots=True)
class FieldConflictDecision[ValueT]:
    """What the resolver decided about one field of one fund."""

    record: ConflictRecord
    selected: tuple[Candidate[ValueT], SourceScope] | None
    alternatives_with_scope: tuple[
        tuple[
            Candidate[ValueT],
            SourceScope,
        ],
        ...,
    ]

    @property
    def alternatives(self) -> list[Candidate[ValueT]]:
        return [candidate for candidate, _ in self.alternatives_with_scope]


def candidate_facts(
    *,
    document: ExtractionDocument,
    quote: str,
    page_number: int | None,
    score: int,
    scope: SourceScope,
    fund_name: str,
    fund_web: str | None,
    priorities: dict[DocumentType, int] | None,
    ledger: ConflictLedger | None,
    display_value: str,
    raw_value: str,
) -> CandidateFacts:
    """Describe one candidate in the terms the comparison understands."""

    record = document.record

    dates = (
        ledger.dates.read(
            source_id=record.source_id,
            url=record.url,
            text=document.document.full_text,
        )
        if ledger is not None
        else UNKNOWN_DATE
    )

    return CandidateFacts(
        display_value=display_value,
        raw_value=raw_value,
        source_id=record.source_id,
        source_url=record.url,
        source_title=record.title,
        document_type=record.document_type,
        quote=quote,
        page=page_number,
        scope=scope.value,
        scope_rank=_scope_rank(scope),
        # The entity of this candidate was already confirmed by the scope
        # rules, so the authority ladder ranks a source that was accepted
        # rather than deciding whether to accept it.
        authority=classify_authority(
            source_url=record.url,
            source_title=record.title,
            fund_name=fund_name,
            fund_web=fund_web,
            names_the_fund=scope in ACCEPTED_SCOPES,
        ),
        document_priority=(
            document_priority(
                document,
                priorities,
            )
            if priorities is not None
            else 0
        ),
        date=dates,
        parser_name=record.parser_name,
        score=score,
    )


def resolve_field_candidates[ValueT](
    *,
    fund_name: str,
    fund_web: str | None,
    field: str,
    ranked_candidates: Sequence[
        tuple[
            Candidate[ValueT],
            SourceScope,
        ]
    ],
    value_key: Callable[[ValueT], Hashable],
    conflict_key: Callable[[ValueT], Hashable] | None,
    display: Callable[[ValueT], str] | None = None,
    priorities: dict[DocumentType, int] | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldConflictDecision[ValueT] | None:
    """
    Compare the candidates that claim the same thing as the leading one.

    Only the group of the leading candidate is contested. A document
    reporting a different date, class or metric makes a different claim,
    and the field reports one of them, not an argument between them.
    """

    leader = ranked_candidates[0]

    group_key = conflict_key(leader[0].value) if conflict_key is not None else None

    group = [
        item
        for item in ranked_candidates
        if conflict_key is None or conflict_key(item[0].value) == group_key
    ]

    if len(group) < 2:
        return None

    def facts_of(
        item: tuple[Candidate[ValueT], SourceScope],
    ) -> CandidateFacts:
        return candidate_facts(
            document=item[0].document,
            quote=item[0].quote,
            page_number=item[0].page_number,
            score=item[0].score,
            scope=item[1],
            fund_name=fund_name,
            fund_web=fund_web,
            priorities=priorities,
            ledger=ledger,
            display_value=(
                display(item[0].value) if display is not None else str(value_key(item[0].value))
            ),
            raw_value=item[0].raw_value,
        )

    resolution: Resolution[tuple[Candidate[ValueT], SourceScope]] = resolve_group(
        fund_name=fund_name,
        field=field,
        semantic_key=str(group_key) if group_key is not None else field,
        items=group,
        facts_of=facts_of,
        value_key=lambda item: value_key(item[0].value),
    )

    # A group of one is not a disagreement and is not reported. A group
    # whose members all say the same thing is: it is what the report
    # counts as equivalent after normalization.
    if ledger is not None:
        ledger.record(resolution.record)

    if resolution.outcome is ConflictOutcome.EQUIVALENT:
        return None

    return FieldConflictDecision(
        record=resolution.record,
        selected=resolution.selected,
        alternatives_with_scope=tuple(
            item for item in resolution.alternatives if item is not resolution.selected
        ),
    )


def _losing_source_attempts[ValueT](
    *,
    candidates: Sequence[Candidate[ValueT]],
    detail: str,
) -> list[SourceAttempt]:
    """Record a candidate that lost a resolved conflict as an attempt."""

    attempts: list[SourceAttempt] = []

    seen: set[str] = set()

    for candidate in candidates:
        record = candidate.document.record

        if record.url in seen:
            continue

        seen.add(record.url)

        attempts.append(
            SourceAttempt(
                url=HttpUrl(record.url),
                retrieved_at=source_datetime(record),
                outcome=ReasonCode.CONFLICTING_VALUES,
                document_type=resolve_document_type(record.document_type),
                detail=f"{candidate.quote[:200]} | not selected: {detail}"[:500],
            )
        )

    return attempts


def fee_collection_key(
    collection: FeeCollection,
) -> Hashable:
    """
    Return what two fee schedules have to agree on to be the same.

    Only the charge itself is compared. The wording of the basis, the
    order of the items and the free text of a condition differ between a
    statute and a price list without the investor paying anything else,
    and comparing them would report every pair of documents as a
    disagreement.
    """

    return frozenset(
        (
            item.type.value,
            number_key(item.rate_percent),
            number_key(item.fixed_amount),
            item.currency or "",
            number_key(item.minimum_rate_percent),
            number_key(item.maximum_rate_percent),
            tuple(
                sorted(
                    (
                        tier.basis.value,
                        tier.from_months,
                        tier.to_months,
                        tier.share_class or "",
                        number_key(tier.rate_percent),
                        number_key(tier.fixed_amount),
                    )
                    for tier in item.tiers
                )
            ),
        )
        for item in collection.items
    )


def describe_fee_collection(
    collection: FeeCollection,
) -> str:
    """Return a fee schedule in the shortest form a reader can compare."""

    parts: list[str] = []

    for item in sorted(
        collection.items,
        key=lambda entry: entry.type.value,
    ):
        if item.rate_percent is not None:
            parts.append(f"{item.type.value} {item.rate_percent:g} %")
        elif item.fixed_amount is not None:
            amount = f"{item.fixed_amount:,.0f} {item.currency or ''}".strip()

            parts.append(f"{item.type.value} {amount}")
        elif item.tiers:
            parts.append(f"{item.type.value} {len(item.tiers)} tiers")
        else:
            parts.append(item.type.value)

    return ", ".join(parts)


def candidate_or_missing[ValueT](
    *,
    fund_name: str,
    candidates: list[Candidate[ValueT]],
    documents: list[ExtractionDocument],
    missing_detail: str,
    detect_conflicts: bool = False,
    value_key: (
        Callable[
            [ValueT],
            Hashable,
        ]
        | None
    ) = None,
    # Step 8. Candidates only contest one another when they claim the
    # same thing. A different reporting date, share class or metric is a
    # different claim, and two of them are not a disagreement.
    conflict_key: (
        Callable[
            [ValueT],
            Hashable,
        ]
        | None
    ) = None,
    # How the value reads in the conflict report. A collection compares
    # as a set of normalized charges, which is precise and unreadable.
    display: Callable[[ValueT], str] | None = None,
    field: str = "",
    fund_web: str | None = None,
    priorities: dict[DocumentType, int] | None = None,
    ledger: ConflictLedger | None = None,
) -> FieldResult[ValueT]:
    if not candidates:
        return missing_result(
            documents=documents,
            detail=missing_detail,
        )

    candidates_with_scope = [
        (
            candidate,
            _scope_verdict(
                candidate=candidate,
                fund_name=fund_name,
            ),
        )
        for candidate in candidates
    ]

    # Only a source whose entity was confirmed may be attributed to this
    # fund. A document that names another fund, describes the manager, or
    # proves no identity at all is rejected instead of being ranked lower.
    eligible_candidates = [
        (
            candidate,
            verdict,
        )
        for candidate, verdict in candidates_with_scope
        if verdict in ACCEPTED_SCOPES
    ]

    if not eligible_candidates:
        return _unconfirmed_scope_result(
            candidates_with_scope=candidates_with_scope,
            documents=documents,
        )

    ranked_candidates = sorted(
        eligible_candidates,
        key=lambda item: (
            _scope_rank(item[1]),
            item[0].score,
            item[0].document.record.source_id,
        ),
        reverse=True,
    )

    best_candidate, best_scope = ranked_candidates[0]

    losing_candidates: tuple[Candidate[ValueT], ...] = ()

    resolution_note = ""

    if detect_conflicts and value_key is not None:
        decision = resolve_field_candidates(
            fund_name=fund_name,
            fund_web=fund_web,
            field=field or "value",
            ranked_candidates=ranked_candidates,
            value_key=value_key,
            conflict_key=conflict_key,
            display=display,
            priorities=priorities,
            ledger=ledger,
        )

        if decision is not None:
            if decision.selected is None:
                return _conflicting_result(
                    best_candidate=best_candidate,
                    conflicting_candidates=list(decision.alternatives),
                    documents=documents,
                    detail=decision.record.reason,
                )

            best_candidate, best_scope = decision.selected

            losing_candidates = tuple(
                candidate for candidate, _ in decision.alternatives_with_scope
            )

            resolution_note = decision.record.reason

    confidence = confidence_from_score(best_candidate.score)

    review_required = confidence is Confidence.LOW or best_scope is SourceScope.SHARE_CLASS

    if best_scope is SourceScope.SHARE_CLASS and confidence is Confidence.HIGH:
        confidence = Confidence.MEDIUM

    source_record = best_candidate.document.record

    # A value read from the layout fallback was found in a text rebuilt
    # from the page rather than read off it. Rebuilding puts words next
    # to each other that were never adjacent, and a label can pick up a
    # number that belonged to a different part of the page: a memorandum
    # measured this way reported a construction progress of 75 % as a
    # guaranteed minimum return. Every such value goes in front of a
    # reviewer, and one that could not even be placed on a page, so that
    # a reader cannot check it against the document, is worth less still.
    if is_anydoc_parser(source_record.parser_name):
        confidence, review_required = fallback_extraction_metadata(
            confidence,
            placed_on_a_page=best_candidate.page_number is not None,
        )

    return FieldResult[ValueT](
        status=FieldStatus.FOUND,
        value=best_candidate.value,
        raw_value=best_candidate.raw_value,
        scope=DataScope(
            type=_scope_type(best_scope),
            fund_name=fund_name,
        ),
        source=Evidence(
            source=SourceMetadata(
                url=HttpUrl(source_record.url),
                document_type=resolve_document_type(source_record.document_type),
                retrieved_at=source_datetime(source_record),
                title=source_record.title,
            ),
            quote=best_candidate.quote,
            page=best_candidate.page_number,
        ),
        extraction=ExtractionMetadata(
            method=ExtractionMethod.REGEX,
            confidence=confidence,
            review_required=review_required,
        ),
        # A candidate that lost a resolved conflict is kept next to the
        # winner, with the value it stated and the reason it lost, so the
        # delivered file never hides that a second source said otherwise.
        attempted_sources=_losing_source_attempts(
            candidates=losing_candidates,
            detail=resolution_note,
        ),
    )


@dataclass(frozen=True, slots=True)
class IsinIdentity:
    """Who an official ISIN belongs to, and at which scope."""

    fund_name: str
    scope: SourceScope
    subfund_name: str | None = None
    share_class_name: str | None = None


# ISIN -> owner. Built from the official CNB register, so an entry is an
# exact statement of ownership, not a guess.
type IsinIdentityIndex = Mapping[str, IsinIdentity]


# The index is supplied for a whole extraction run rather than threaded
# through every field function, because identity is a property of the run
# and not of one field. Unset, it is None, and every identity decision is
# taken exactly as it was before the index existed.
_ISIN_IDENTITY: Final[ContextVar[IsinIdentityIndex | None]] = ContextVar(
    "fundscraper_isin_identity",
    default=None,
)


@contextmanager
def official_isin_identity(
    index: IsinIdentityIndex | None,
) -> Iterator[None]:
    """
    Make an official ISIN index available to identity decisions.

    Entering with None - or with an empty index - changes nothing, so a
    caller that has no register behaves exactly as before.
    """

    token = _ISIN_IDENTITY.set(index or None)

    try:
        yield
    finally:
        _ISIN_IDENTITY.reset(token)


def active_isin_identity() -> IsinIdentityIndex | None:
    """Return the index of the current extraction run, if one was set."""

    return _ISIN_IDENTITY.get()


# An ISIN as it is printed in a document: two country letters, nine
# alphanumerics and a check digit.
_ISIN_IN_TEXT: Final = re.compile(r"\b([A-Z]{2}[0-9A-Z]{9}[0-9])\b")

# Only these scopes may be asserted by an ISIN. An ISIN identifies a
# security, so it can prove the fund, the subfund or the share class it
# was issued to - never a manager-level or generic document.
_ISIN_ASSERTABLE: Final[frozenset[SourceScope]] = frozenset(
    {
        SourceScope.EXACT_FUND,
        SourceScope.SUBFUND,
        SourceScope.SHARE_CLASS,
    }
)


# Canonical host -> the one fund whose official website it is. Built from
# the canonical input, so an entry means the operator recorded that host
# as this fund's own site and no other fund claims it.
type OfficialSiteIndex = Mapping[str, str]


_OFFICIAL_SITE: Final[ContextVar[OfficialSiteIndex | None]] = ContextVar(
    "fundscraper_official_site",
    default=None,
)


def build_official_site_index(
    funds: Iterable[FundInput],
) -> dict[str, str]:
    """
    Return the hosts that are one fund's own official website.

    A host qualifies only when the canonical input points one fund at its
    bare root and no other fund points anywhere on it. Both halves are
    needed: an administrator hub is the recorded website of dozens of
    funds, and a fund whose entry is a page inside such a hub owns that
    page, not the hub.
    """

    claims: dict[str, list[FundInput]] = {}

    for fund in funds:
        if not fund.web:
            continue

        claims.setdefault(canonical_domain(fund.web), []).append(fund)

    index: dict[str, str] = {}

    for host, claimants in claims.items():
        if not host or len(claimants) != 1:
            continue

        parts = urlsplit(claimants[0].web or "")

        if parts.path.strip("/") or parts.query:
            continue

        index[host] = claimants[0].name

    return index


@contextmanager
def official_site_identity(
    index: OfficialSiteIndex | None,
) -> Iterator[None]:
    """
    Make the official-website register available to identity decisions.

    Entering with None - or with an empty index - changes nothing, so a
    caller without the canonical input behaves exactly as before.
    """

    token = _OFFICIAL_SITE.set(index or None)

    try:
        yield
    finally:
        _OFFICIAL_SITE.reset(token)


def active_official_site() -> OfficialSiteIndex | None:
    """Return the register of the current extraction run, if one was set."""

    return _OFFICIAL_SITE.get()


def official_site_owns_page(
    *,
    fund_name: str,
    source_url: str,
    official_site: OfficialSiteIndex | None = None,
) -> bool:
    """
    Return whether a page is served from this fund's own official website.

    This settles only whose page it is. It says nothing about what a value
    on the page means, and it does not make a section of the page that
    presents a different fund belong to this one.
    """

    index = official_site if official_site is not None else _OFFICIAL_SITE.get()

    if not index or not source_url:
        return False

    return index.get(canonical_domain(source_url)) == fund_name


def official_isin_scope(
    *,
    fund_name: str,
    source_url: str,
    source_title: str | None,
    document_text: str,
    isin_identity: IsinIdentityIndex | None,
) -> SourceScope | None:
    """
    Resolve identity from an official ISIN printed in the source.

    Returns the scope the ISIN was issued at when the document carries an
    official ISIN of this fund, and None when the question cannot be
    settled that way - no index, no ISIN, an unknown ISIN, or an ISIN
    that belongs to a different fund. In every one of those cases the
    caller falls back to name-based identity, unchanged.

    This answers only "whose source is this". It says nothing about what
    a value inside the source means: a per-share value does not become
    fund assets, and a share-class fee does not become a fund-level fee,
    merely because the owner is certain.
    """

    index = isin_identity if isin_identity is not None else _ISIN_IDENTITY.get()

    if not index:
        return None

    haystack = " ".join(
        part for part in (source_title or "", source_url, document_text) if part
    ).upper()

    if "CZ" not in haystack and "LU" not in haystack and "IE" not in haystack:
        return None

    mine: SourceScope | None = None

    for token in set(_ISIN_IN_TEXT.findall(haystack)):
        owner = index.get(token)

        if owner is None:
            continue

        if owner.fund_name != fund_name:
            # The document carries the ISIN of a different fund. That is
            # evidence against this fund, so nothing is asserted here and
            # the name-based rules decide.
            return None

        if owner.scope not in _ISIN_ASSERTABLE:
            continue

        # Several classes of one fund can appear in the same document.
        # The narrowest scope wins, so a share-class KID stays a
        # share-class source even when the fund's own ISIN is printed
        # next to it.
        if mine is None or _scope_rank(owner.scope) < _scope_rank(mine):
            mine = owner.scope

    return mine


def _scope_verdict[ValueT](
    *,
    candidate: Candidate[ValueT],
    fund_name: str,
) -> SourceScope:
    """Classify which entity the source of one candidate describes."""

    record = candidate.document.record

    return classify_source_scope(
        fund_name=fund_name,
        source_url=record.url,
        source_title=record.title,
        document_text=candidate.document.document.full_text,
        quote=candidate.quote,
        value_offset=candidate.value_offset,
        document=candidate.document,
    )


def classify_source_scope(
    *,
    fund_name: str,
    source_url: str,
    source_title: str | None,
    document_text: str,
    quote: str,
    value_offset: int | None = None,
    document: ExtractionDocument | None = None,
    isin_identity: IsinIdentityIndex | None = None,
) -> SourceScope:
    """
    Determine whether a source really belongs to the requested fund.

    Identity is proven by the file name, the link text or the beginning
    of the document, which is where the legal fund name appears. When a
    document names some fund but not this one, it belongs to another
    fund and must never be used.

    An official ISIN, when one is supplied through `isin_identity`, is
    stronger evidence than any of that: a subfund KID states the ISIN of
    the class it prices but rarely repeats the parent fund's full legal
    name, so requiring the name as well loses a source whose owner is
    already certain.
    """

    normalized_quote = normalize_search_text(quote)

    if any(keyword in normalized_quote for keyword in SCOPE_MISMATCH_KEYWORDS):
        return SourceScope.MANAGER

    official_scope = official_isin_scope(
        fund_name=fund_name,
        source_url=source_url,
        source_title=source_title,
        document_text=document_text,
        isin_identity=isin_identity,
    )

    if official_scope is not None:
        return official_scope

    fund_tokens = fund_identity_tokens(fund_name)

    identity_text = normalize_search_text(
        " ".join(
            (
                source_title or "",
                source_url,
                document_text[:IDENTITY_TEXT_CHARACTERS],
            )
        )
    )

    if not fund_tokens:
        # A name built only from generic fund wording cannot be matched
        # by tokens, so no source can be attributed to it with certainty.
        return SourceScope.GENERIC

    if document is not None:
        normalized_document, _ = normalized_document_lines(document)
    else:
        normalized_document = normalize_search_text(document_text)

    # The page of a fund's own website belongs to that fund even where
    # its text names the depositary, the manager or itself in a declined
    # form the mention parser cannot match. The address of a document
    # inside a fund's own path says the same thing.
    owns_the_page = url_identifies_fund(
        fund_tokens=fund_tokens,
        source_url=source_url,
    ) or official_site_owns_page(
        fund_name=fund_name,
        source_url=source_url,
    )

    if names_another_fund(
        text=normalized_document,
        fund_tokens=fund_tokens,
    ):
        # The document also names a different fund, so naming this fund
        # anywhere on it proves nothing. The value must come from the
        # section of this fund, not from a neighbouring one.
        return _section_scope(
            fund_tokens=fund_tokens,
            normalized_document=normalized_document,
            identity_text=identity_text,
            normalized_quote=normalized_quote,
            value_offset=value_offset,
            owns_the_page=owns_the_page,
        )

    confirms_fund, names_some_fund = _named_fund_matches(
        text=identity_text,
        fund_tokens=fund_tokens,
    )

    if confirms_fund:
        return _confirmed_scope(
            identity_text=identity_text,
            normalized_quote=normalized_quote,
        )

    if names_some_fund:
        # The document states a legal fund name and it is not this one.
        return SourceScope.OTHER_FUND

    if _identifies_fund(
        fund_tokens=fund_tokens,
        text=identity_text,
    ):
        return _confirmed_scope(
            identity_text=identity_text,
            normalized_quote=normalized_quote,
        )

    if owns_the_page:
        return _confirmed_scope(
            identity_text=identity_text,
            normalized_quote=normalized_quote,
        )

    return SourceScope.GENERIC


def _section_scope(
    *,
    fund_tokens: tuple[str, ...],
    normalized_document: str,
    identity_text: str,
    normalized_quote: str,
    value_offset: int | None,
    owns_the_page: bool = False,
) -> SourceScope:
    """Accept a value of a multi-fund page only from this fund's section."""

    if value_offset is None:
        # Without the position of the value it cannot be told apart from
        # the value of a neighbouring fund on the same page.
        return SourceScope.OTHER_FUND

    if owns_the_page:
        # The document is filed under the address of this fund, so its
        # content belongs to it except where another fund is presented.
        # This is what keeps the detail page of a fund usable when the
        # navigation of the site lists every other fund of the manager.
        foreign = _foreign_sections(
            text=normalized_document,
            fund_tokens=fund_tokens,
        )

        if any(start <= value_offset < end for start, end in foreign):
            return SourceScope.OTHER_FUND

        return _confirmed_scope(
            identity_text=identity_text,
            normalized_quote=normalized_quote,
        )

    spans = fund_sections(
        text=normalized_document,
        fund_tokens=fund_tokens,
    )

    if not spans:
        return SourceScope.OTHER_FUND

    if not any(start <= value_offset < end for start, end in spans):
        return SourceScope.OTHER_FUND

    return _confirmed_scope(
        identity_text=identity_text,
        normalized_quote=normalized_quote,
    )


def url_identifies_fund(
    *,
    fund_tokens: tuple[str, ...],
    source_url: str,
) -> bool:
    """
    Return whether a URL path segment is the slug of this exact fund.

    Matching complete tokens of one segment keeps "care-sicav-a-s" for
    CARE SICAV while refusing "esg-seniorcare" and "alca-podfond-caresort".
    """

    if not fund_tokens or not source_url:
        return False

    for segment in urlsplit(source_url).path.split("/"):
        if not segment:
            continue

        words = re.findall(
            r"[a-z0-9]+",
            normalize_search_text(segment.replace("-", " ").replace("_", " ")),
        )

        if all(token in words for token in fund_tokens):
            return True

    return False


def foreign_section_ratio(
    *,
    text: str,
    fund_name: str,
) -> float:
    """
    Return the share of a document that presents some fund other than this.

    A value found inside such a span is refused, so this is what a
    document is worth to the attribution rules. It is measured rather
    than assumed because a reading that drops the running page header
    loses the confirmation that ends each foreign span, which can turn
    most of a document a fund published about itself into a section that
    appears to belong to somebody else.
    """

    if not text:
        return 0.0

    fund_tokens = fund_identity_tokens(fund_name)

    if not fund_tokens:
        return 0.0

    spans = _foreign_sections(
        text=text,
        fund_tokens=fund_tokens,
    )

    if not spans:
        return 0.0

    covered = 0

    highest_end = 0

    for start, end in sorted(spans):
        if end <= highest_end:
            continue

        covered += end - max(start, highest_end)

        highest_end = end

    return covered / len(text)


def _foreign_sections(
    *,
    text: str,
    fund_tokens: tuple[str, ...],
) -> tuple[tuple[int, int], ...]:
    """Return the spans presenting a fund other than the requested one."""

    mentions = iter_named_funds(text)

    spans: list[tuple[int, int]] = []

    for index, mention in enumerate(mentions):
        if _mention_matches(
            mention=mention,
            fund_tokens=fund_tokens,
        ):
            continue

        end = len(text)

        for following in mentions[index + 1 :]:
            if following.start > mention.start:
                end = following.start

                break

        spans.append(
            (
                mention.start,
                max(end, mention.end),
            )
        )

    return tuple(spans)


def _confirmed_scope(
    *,
    identity_text: str,
    normalized_quote: str,
) -> SourceScope:
    if any(keyword in normalized_quote for keyword in SHARE_CLASS_KEYWORDS):
        return SourceScope.SHARE_CLASS

    if any(keyword in identity_text for keyword in SUBFUND_KEYWORDS):
        return SourceScope.SUBFUND

    return SourceScope.EXACT_FUND


@dataclass(frozen=True, slots=True)
class NamedFundMention:
    """One legal fund name stated in a document, with its position."""

    tokens: tuple[str, ...]
    start: int
    end: int


WORD_PATTERN: Final = re.compile(r"[a-z0-9]+")


# Every candidate of a fund resolves its scope against the same document,
# so the fund names of a text are located once instead of once per value.
_NAMED_FUNDS: dict[
    tuple[int, int],
    tuple[str, tuple[NamedFundMention, ...]],
] = {}


_NAMED_FUNDS_LIMIT: Final = 256


def iter_named_funds(
    text: str,
) -> tuple[NamedFundMention, ...]:
    """Return every legal fund name stated in a normalized text."""

    cache_key = (
        len(text),
        hash(text),
    )

    cached = _NAMED_FUNDS.get(cache_key)

    if cached is not None and cached[0] == text:
        return cached[1]

    # The words of the document are located once. Scanning the whole
    # prefix again for every legal form made this quadratic, which on a
    # long statute cost minutes per fund.
    words = [(match.group(0), match.start()) for match in WORD_PATTERN.finditer(text)]

    word_starts = [start for _, start in words]

    mentions: list[NamedFundMention] = []

    for match in FUND_ENTITY_KEYWORD_PATTERN.finditer(text):
        end_index = bisect_left(
            word_starts,
            match.start(),
        )

        window = words[max(0, end_index - FUND_NAME_WINDOW_WORDS) : end_index]

        name_words = [
            (word, position)
            for word, position in window
            if word not in FUND_NAME_NOISE_TOKENS and len(word) >= 2
        ]

        if not name_words:
            continue

        mentions.append(
            NamedFundMention(
                tokens=tuple(word for word, _ in name_words),
                start=name_words[0][1],
                end=match.end(),
            )
        )

    result = tuple(mentions)

    if len(_NAMED_FUNDS) >= _NAMED_FUNDS_LIMIT:
        _NAMED_FUNDS.clear()

    _NAMED_FUNDS[cache_key] = (
        text,
        result,
    )

    return result


def fund_sections(
    *,
    text: str,
    fund_tokens: tuple[str, ...],
) -> tuple[tuple[int, int], ...]:
    """
    Return the spans of a multi-fund page that belong to this fund.

    A manager page lists many funds one after another. The section of a
    fund starts at its name and ends where a different fund is named.
    Repeating the name of the same fund does not close its section,
    because a statute names its fund on almost every page.
    """

    mentions = iter_named_funds(text)

    spans: list[tuple[int, int]] = []

    for index, mention in enumerate(mentions):
        if not _mention_matches(
            mention=mention,
            fund_tokens=fund_tokens,
        ):
            continue

        end = len(text)

        for following in mentions[index + 1 :]:
            if not _mention_matches(
                mention=following,
                fund_tokens=fund_tokens,
            ):
                end = following.start

                break

        spans.append(
            (
                mention.start,
                max(end, mention.end),
            )
        )

    return tuple(spans)


def names_another_fund(
    *,
    text: str,
    fund_tokens: tuple[str, ...],
) -> bool:
    """
    Return whether a text names a fund other than the requested one.

    Only a foreign name makes a document ambiguous. One fund named
    several times, for example as "Statut KOOR ESG fond" and later as
    "KOOR ESG SICAV a.s.", is still a single-fund document.
    """

    return any(
        not _mention_matches(
            mention=mention,
            fund_tokens=fund_tokens,
        )
        for mention in iter_named_funds(text)
    )


def section_boundaries(
    text: str,
) -> tuple[int, ...]:
    """Return the start offset of every fund section of a document."""

    return tuple(mention.start for mention in iter_named_funds(text))


def section_index(
    *,
    boundaries: tuple[int, ...],
    offset: int,
) -> int:
    """Return the index of the section an offset belongs to."""

    index = -1

    for position, boundary in enumerate(boundaries):
        if boundary <= offset:
            index = position
        else:
            break

    return index


def _mention_matches(
    *,
    mention: NamedFundMention,
    fund_tokens: tuple[str, ...],
) -> bool:
    return len(mention.tokens) >= len(fund_tokens) and (
        list(mention.tokens[-len(fund_tokens) :]) == list(fund_tokens)
    )


def _named_fund_matches(
    *,
    text: str,
    fund_tokens: tuple[str, ...],
) -> tuple[bool, bool]:
    """
    Compare the legal fund names stated in a document with this fund.

    The name of a fund stands immediately in front of its legal form, so
    only the words in that position are compared. Counting shared words
    anywhere in the document cannot separate "CREDITAS ASSETS SICAV"
    from "CREDITAS fond SICAV".
    """

    names_some_fund = False

    for match in FUND_ENTITY_KEYWORD_PATTERN.finditer(text):
        preceding_words = re.findall(
            r"[a-z0-9]+",
            text[: match.start()],
        )[-FUND_NAME_WINDOW_WORDS:]

        name_words = [
            word
            for word in preceding_words
            if word not in FUND_NAME_NOISE_TOKENS and len(word) >= 2
        ]

        if not name_words:
            continue

        names_some_fund = True

        if len(name_words) >= len(fund_tokens) and (
            name_words[-len(fund_tokens) :] == list(fund_tokens)
        ):
            return (
                True,
                True,
            )

    return (
        False,
        names_some_fund,
    )


def _identifies_fund(
    *,
    fund_tokens: tuple[str, ...],
    text: str,
) -> bool:
    """Return whether the text proves the source belongs to this fund."""

    matched_tokens = sum(1 for token in fund_tokens if token in text)

    required_matches = (
        1
        if len(fund_tokens) == 1
        else max(
            2,
            (len(fund_tokens) + 1) // 2,
        )
    )

    return matched_tokens >= required_matches


def fund_identity_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    normalized = normalize_search_text(fund_name)

    raw_tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    result: list[str] = []

    for token in raw_tokens:
        if token in FUND_NAME_NOISE_TOKENS:
            continue

        if len(token) < 2:
            continue

        if token not in result:
            result.append(token)

    return tuple(result)


SCOPE_RANKS: Final[dict[SourceScope, int]] = {
    SourceScope.EXACT_FUND: 3,
    SourceScope.SUBFUND: 2,
    SourceScope.SHARE_CLASS: 1,
}


SCOPE_TYPES: Final[dict[SourceScope, ScopeType]] = {
    SourceScope.EXACT_FUND: ScopeType.FUND,
    SourceScope.SUBFUND: ScopeType.SUBFUND,
    SourceScope.SHARE_CLASS: ScopeType.SHARE_CLASS,
    SourceScope.MANAGER: ScopeType.MANAGER,
}


def _scope_rank(
    verdict: SourceScope,
) -> int:
    return SCOPE_RANKS.get(
        verdict,
        0,
    )


def _scope_type(
    verdict: SourceScope,
) -> ScopeType:
    return SCOPE_TYPES.get(
        verdict,
        ScopeType.UNKNOWN,
    )


def _find_conflicting_candidates[ValueT](
    *,
    best_candidate: Candidate[ValueT],
    best_scope: SourceScope,
    ranked_candidates: Sequence[
        tuple[
            Candidate[ValueT],
            SourceScope,
        ]
    ],
    value_key: Callable[
        [ValueT],
        Hashable,
    ],
) -> list[Candidate[ValueT]]:
    best_key = value_key(best_candidate.value)

    conflicts: list[Candidate[ValueT]] = []

    for candidate, scope in ranked_candidates[1:]:
        if candidate.document.record.source_id == best_candidate.document.record.source_id:
            continue

        if scope is not best_scope:
            continue

        if candidate.score < best_candidate.score - 20:
            continue

        if value_key(candidate.value) == best_key:
            continue

        conflicts.append(candidate)

    return conflicts


def _unconfirmed_scope_result[ValueT](
    *,
    candidates_with_scope: list[tuple[Candidate[ValueT], SourceScope]],
    documents: list[ExtractionDocument],
) -> FieldResult[ValueT]:
    """
    Reject values whose source could not be tied to the requested fund.

    The reason distinguishes a manager-level statement, a document of a
    different fund, and a document that proves no entity at all, because
    each of them needs a different correction.
    """

    scopes = {scope for _, scope in candidates_with_scope}

    candidates = [candidate for candidate, _ in candidates_with_scope]

    if scopes == {SourceScope.MANAGER}:
        reason_code = ReasonCode.SCOPE_MISMATCH

        detail = (
            "Quantified values were found, but their evidence "
            "appears to describe the investment manager, group "
            "or multiple funds rather than the exact fund."
        )
    elif SourceScope.OTHER_FUND in scopes:
        reason_code = ReasonCode.ENTITY_NOT_MATCHED

        detail = (
            "Quantified values were found, but every source names a "
            "different fund. The documents are hosted together with "
            "this fund and do not describe it."
        )
    else:
        reason_code = ReasonCode.ENTITY_NOT_MATCHED

        detail = (
            "Quantified values were found, but no source proved that it "
            "belongs to this exact fund, subfund or share class."
        )

    return FieldResult[ValueT](
        status=FieldStatus.AMBIGUOUS,
        reason=MissingReason(
            code=reason_code,
            detail=detail,
        ),
        attempted_sources=_candidate_source_attempts(
            candidates=candidates,
            fallback_documents=documents,
            outcome=reason_code,
        ),
    )


def _conflicting_result[ValueT](
    *,
    best_candidate: Candidate[ValueT],
    conflicting_candidates: list[Candidate[ValueT]],
    documents: list[ExtractionDocument],
    detail: str = "",
) -> FieldResult[ValueT]:
    all_candidates = [
        best_candidate,
        *conflicting_candidates,
    ]

    return FieldResult[ValueT](
        status=FieldStatus.CONFLICTING,
        reason=MissingReason(
            code=ReasonCode.CONFLICTING_VALUES,
            detail=(
                detail
                or (
                    "Multiple similarly reliable fund-level sources "
                    "contain materially different values. The field "
                    "requires source-date, share-class or manual review."
                )
            ),
        ),
        attempted_sources=_candidate_source_attempts(
            candidates=all_candidates,
            fallback_documents=documents,
            outcome=ReasonCode.CONFLICTING_VALUES,
        ),
    )


def _candidate_source_attempts[ValueT](
    *,
    candidates: list[Candidate[ValueT]],
    fallback_documents: list[ExtractionDocument],
    outcome: ReasonCode,
) -> list[SourceAttempt]:
    attempts: list[SourceAttempt] = []

    seen_urls: set[str] = set()

    for candidate in candidates:
        record = candidate.document.record

        if record.url in seen_urls:
            continue

        seen_urls.add(record.url)

        attempts.append(
            SourceAttempt(
                url=HttpUrl(record.url),
                retrieved_at=source_datetime(record),
                outcome=outcome,
                document_type=resolve_document_type(record.document_type),
                detail=candidate.quote,
            )
        )

    if attempts:
        return attempts

    return [
        SourceAttempt(
            url=HttpUrl(document.record.url),
            retrieved_at=source_datetime(document.record),
            outcome=outcome,
            document_type=resolve_document_type(document.record.document_type),
            detail=("The source was reviewed during scope and conflict validation."),
        )
        for document in fallback_documents
    ]


def missing_result[ValueT](
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
                retrieved_at=source_datetime(document.record),
                outcome=reason_code,
                document_type=resolve_document_type(document.record.document_type),
                detail=detail,
            )
            for document in documents
        ],
    )


def source_datetime(
    record: ParsedDocumentRecord,
) -> datetime:
    raw_value = record.retrieved_at or record.parsed_at

    parsed = datetime.fromisoformat(raw_value)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed


def confidence_from_score(
    score: int,
) -> Confidence:
    if score >= 145:
        return Confidence.HIGH

    if score >= 105:
        return Confidence.MEDIUM

    return Confidence.LOW


def fallback_confidence(
    confidence: Confidence,
    *,
    placed_on_a_page: bool,
) -> Confidence:
    """Return what a value read from a rebuilt text is worth."""

    reduced, _ = fallback_extraction_metadata(
        confidence,
        placed_on_a_page=placed_on_a_page,
    )

    return reduced
