"""
Extraction of the fields added in schema version 3.

The module produces the manager, the administrator, the assets history,
the annual returns, the historical values and the news of one fund from
the documents a previous run parsed. It reuses the scope confirmation,
the evidence and the conflict handling of
:mod:`fundscraper.field_extraction`, so a value of a new field is
accepted under exactly the rules that already govern the delivered ones.

No network access happens here. The news of a fund are read from the
downloaded body of its own pages, which the processing database stores
next to the parsed text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from html import unescape
from pathlib import Path
from typing import Final
from urllib.parse import SplitResult, urlsplit

from pydantic import HttpUrl, ValidationError

from fundscraper.anydoc_parser import is_anydoc_parser
from fundscraper.extended_validation import (
    ValidationFinding,
    ValidationSeverity,
    fallback_extraction_metadata,
    rejects,
    validate_annual_returns,
    validate_capital_observations,
    validate_historical_series,
    validate_news_items,
)
from fundscraper.field_definitions import (
    CAPITAL_METRIC_BY_LABEL,
    COMPANY_NAME_PATTERN,
    ICO_PATTERN,
    MANAGEMENT_COMPANY_PATTERN,
    NEWS_TITLE_MAXIMUM_CHARACTERS,
    NON_FUND_CAPITAL_METRICS,
    PARTY_LABEL_MAXIMUM_DISTANCE,
    PARTY_ROLE_LABELS,
    PAST_RETURN_MARKERS,
    classify_capital_metric,
    classify_historical_value,
    classify_return_series,
    clean_party_name,
    declared_frequency,
    describes_manager_level_capital,
    describes_statutory_capital,
    describes_value_per_share,
    frequency_from_gap_days,
    is_news_article_path,
    is_news_link_noise,
    is_news_path,
    normalize_ico,
    share_class_code,
)
from fundscraper.field_extraction import (
    ACCEPTED_SCOPES,
    AUM_LABEL_MAXIMUM_DISTANCE,
    DATE_ISO_PATTERN,
    DATE_NUMERIC_PATTERN,
    MONEY_PATTERN,
    NUMBER_PATTERN,
    Candidate,
    ExtractionDocument,
    SourceScope,
    candidate_or_missing,
    classify_source_scope,
    confidence_from_score,
    declared_multiplier,
    document_priority,
    extract_date,
    fund_identity_tokens,
    missing_result,
    normalize_currency,
    normalized_document_lines,
    parse_money_amount,
    parse_number,
    resolve_document_type,
    safe_date,
    source_datetime,
)
from fundscraper.html_discovery import decode_html_bytes, normalize_search_text
from fundscraper.normalization import canonical_domain
from fundscraper.output_models import (
    AnnualReturnHistory,
    AnnualReturnObservation,
    AumHistory,
    AumMetricType,
    CapitalObservation,
    Confidence,
    DataScope,
    DocumentType,
    Evidence,
    ExtractionMetadata,
    ExtractionMethod,
    FieldResult,
    FieldStatus,
    FundNewsCollection,
    FundNewsItem,
    FundParty,
    HistoricalValueCollection,
    HistoricalValueObservation,
    HistoricalValueSeries,
    HistoricalValueType,
    MissingReason,
    NewsSourceType,
    PartyRole,
    ReasonCode,
    ReturnSeriesType,
    ScopeType,
    SeriesFrequency,
    SourceAttempt,
    SourceMetadata,
)
from fundscraper.table_extraction import iter_table_values

PARTY_DOCUMENT_PRIORITY: Final = {
    DocumentType.STATUTE: 90,
    DocumentType.SUBFUND_STATUTE: 90,
    DocumentType.ANNUAL_REPORT: 80,
    DocumentType.PRIIPS_KID: 75,
    DocumentType.MEMORANDUM: 70,
    DocumentType.FACTSHEET: 60,
    DocumentType.MARKETING_PAGE: 50,
}


CAPITAL_DOCUMENT_PRIORITY: Final = {
    DocumentType.ANNUAL_REPORT: 90,
    DocumentType.FINANCIAL_STATEMENTS: 90,
    DocumentType.HALF_YEAR_REPORT: 80,
    DocumentType.FACTSHEET: 70,
    DocumentType.INFOLETTER: 60,
    DocumentType.MARKETING_PAGE: 40,
}


RETURN_DOCUMENT_PRIORITY: Final = {
    DocumentType.ANNUAL_REPORT: 85,
    DocumentType.FACTSHEET: 85,
    DocumentType.HALF_YEAR_REPORT: 75,
    DocumentType.INFOLETTER: 70,
    DocumentType.PRIIPS_KID: 60,
    DocumentType.MARKETING_PAGE: 55,
}


# A percentage of a performance table may be negative, which the percent
# pattern of the delivered fields never had to allow. A PDF prints the
# minus sign as a hyphen or as a typographic minus.
SIGNED_PERCENT_PATTERN: Final = re.compile(
    rf"(?P<value>[-+−]?\s?{NUMBER_PATTERN})\s*%",
)


YEAR_PATTERN: Final = re.compile(r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?!\d)")


# How far from its year the performance of that year may stand. A table
# row prints them next to each other; a paragraph one sentence apart
# already risks pairing the wrong two numbers.
RETURN_YEAR_MAXIMUM_DISTANCE: Final = 25


# A calendar year of a fund report. Values outside it are a page number,
# an amount or a typographic artefact rather than a reporting year.
EARLIEST_REPORTING_YEAR: Final = 1990

LATEST_REPORTING_YEAR: Final = 2100


# A performance beyond this is a cumulative figure, a multiple or a
# broken layout, never the result of one calendar year.
ANNUAL_RETURN_LIMIT_PERCENT: Final = 300.0


# The value of one investment share is a small number and a capital
# figure is a large one. The bound keeps a share value out of a capital
# series and a capital figure out of a share series.
SHARE_VALUE_MAXIMUM: Final = 100_000.0


# How many observations of one field are kept. A daily value page of a
# large fund would otherwise put thousands of rows into one output file.
MAXIMUM_SERIES_OBSERVATIONS: Final = 400


MAXIMUM_NEWS_ITEMS: Final = 25


# How many lines a heading keeps governing the rows below it. A table
# names its quantity once, in the row above the data, but the name must
# not travel through the rest of the document: that is how a registered
# capital of a later paragraph ended up in a net-assets series.
HEADING_MEMORY_LINES: Final = 12


# A value belongs to a table, not to a paragraph. A sentence about
# interest rates names a year and a percentage just as a performance row
# does, and reading it produced returns such as "2024: 2 %" taken from a
# commentary on inflation. Counting the words of the line separates the
# two without needing the layout of the original document.
MAXIMUM_ROW_WORDS: Final = 20

MAXIMUM_TABLE_ROW_WORDS: Final = 8

MAXIMUM_CAPITAL_SENTENCE_WORDS: Final = 40


@dataclass(frozen=True, slots=True)
class ExtendedFundFields:
    """The fields added to the output in schema version 3."""

    manager: FieldResult[FundParty]

    administrator: FieldResult[FundParty]

    aum_history: FieldResult[AumHistory]

    annual_returns: FieldResult[AnnualReturnHistory]

    historical_values: FieldResult[HistoricalValueCollection]

    news: FieldResult[FundNewsCollection]


def extract_extended_fields(
    *,
    fund_name: str,
    fund_web: str | None,
    documents: list[ExtractionDocument],
) -> ExtendedFundFields:
    """Extract every field added in schema version 3 for one fund."""

    return ExtendedFundFields(
        manager=extract_party(
            fund_name=fund_name,
            documents=documents,
            role=PartyRole.MANAGER,
        ),
        administrator=extract_party(
            fund_name=fund_name,
            documents=documents,
            role=PartyRole.ADMINISTRATOR,
        ),
        aum_history=extract_aum_history(
            fund_name=fund_name,
            documents=documents,
        ),
        annual_returns=extract_annual_returns(
            fund_name=fund_name,
            documents=documents,
        ),
        historical_values=extract_historical_values(
            fund_name=fund_name,
            documents=documents,
        ),
        news=extract_fund_news(
            fund_name=fund_name,
            fund_web=fund_web,
            documents=documents,
        ),
    )


# ---------------------------------------------------------------------------
# Text folding that keeps offsets
# ---------------------------------------------------------------------------


def fold_aligned(
    value: str,
) -> str:
    """
    Fold text for matching while keeping every character in place.

    ``normalize_search_text`` collapses whitespace, so an offset found in
    its result no longer points at the same character of the original.
    A role label has to be located in the original text here, because the
    company name that follows it is stored with its diacritics.
    """

    characters: list[str] = []

    for character in value:
        if character.isspace():
            characters.append(" ")

            continue

        decomposed = unicodedata.normalize(
            "NFKD",
            character,
        )

        base = "".join(item for item in decomposed if not unicodedata.combining(item))

        # A ligature decomposes into several letters. Only the first one
        # is kept so that one input character stays one output character.
        folded = (base[:1] or character).lower()

        characters.append(folded[:1] or character)

    return "".join(characters)


def find_label(
    *,
    folded: str,
    label: str,
    start: int = 0,
) -> tuple[int, int] | None:
    """Return the span of a label inside folded, offset-aligned text."""

    pattern = re.escape(label).replace(
        r"\ ",
        r"\s+",
    )

    match = re.compile(pattern).search(
        folded,
        start,
    )

    if match is None:
        return None

    return (
        match.start(),
        match.end(),
    )


# ---------------------------------------------------------------------------
# Candidates of the collection fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeriesCandidate:
    """One observation together with the evidence that carries it."""

    quote: str
    page_number: int | None
    document: ExtractionDocument
    offset: int
    score: int


@dataclass(frozen=True, slots=True)
class CapitalCandidate(SeriesCandidate):
    observation: CapitalObservation


@dataclass(frozen=True, slots=True)
class ReturnCandidate(SeriesCandidate):
    observation: AnnualReturnObservation


@dataclass(frozen=True, slots=True)
class HistoricalCandidate(SeriesCandidate):
    value_type: HistoricalValueType
    share_class: str | None
    currency: str
    observation: HistoricalValueObservation
    context: str


# ---------------------------------------------------------------------------
# 1. Manager and administrator
# ---------------------------------------------------------------------------


def extract_party(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
    role: PartyRole,
) -> FieldResult[FundParty]:
    """Extract the company acting for the fund in one role."""

    labels = next(
        (labels for candidate_role, labels in PARTY_ROLE_LABELS if candidate_role is role),
        (),
    )

    fund_tokens = fund_identity_tokens(fund_name)

    candidates: list[Candidate[FundParty]] = []

    for extraction_document in documents:
        _, document_lines = normalized_document_lines(extraction_document)

        for index, (line, page_number, offset) in enumerate(document_lines):
            # The label often stands alone on its line and the company
            # follows on the next one, so both are read together.
            block = "\n".join(item for item, _, _ in document_lines[index : index + 3])

            folded = fold_aligned(block)

            span = _first_label_span(
                folded=folded,
                labels=labels,
            )

            if span is None:
                continue

            name = _company_after_label(
                block=block,
                start=_end_of_word(
                    folded=folded,
                    offset=span[1],
                ),
            )

            if name is None:
                continue

            if _names_the_fund_itself(
                name=name,
                fund_tokens=fund_tokens,
            ):
                # The sentence names the fund, not the company acting for
                # it. Storing it would make every fund its own manager.
                continue

            party = _build_party(
                role=role,
                name=name,
                block=block,
            )

            if party is None:
                continue

            score = (
                document_priority(
                    extraction_document,
                    PARTY_DOCUMENT_PRIORITY,
                )
                + 70
            )

            if party.ico is not None:
                score += 15

            if "investicni spolecnost" in normalize_search_text(party.name):
                # A Czech fund is run and administered by an "investicni
                # spolecnost". Ranking that wording above every other
                # company of the same paragraph keeps a depositary bank
                # or a securities dealer from contesting the manager.
                score += 25

            candidates.append(
                Candidate(
                    value=party,
                    raw_value=line,
                    quote=block,
                    page_number=page_number,
                    document=extraction_document,
                    score=score,
                    value_offset=offset,
                )
            )

    return candidate_or_missing(
        fund_name=fund_name,
        candidates=candidates,
        documents=documents,
        missing_detail=(
            f"No company acting as the {role.value} of the fund was named "
            "in the parsed public sources."
        ),
        # The name identifies the company. A registration number is only
        # printed by some of the documents, so comparing it as well
        # reported one company as two conflicting values.
        detect_conflicts=True,
        value_key=lambda value: _company_key(value.name),
    )


def _company_key(
    name: str,
) -> str:
    """
    Return a comparable form of a company name.

    Documents of one manager write its legal form as "a.s.", "a. s." and
    "a.s". Comparing the text verbatim reported the same company as two
    conflicting values.
    """

    return "".join(
        re.findall(
            r"[a-z0-9]+",
            normalize_search_text(name),
        )
    )


def _end_of_word(
    *,
    folded: str,
    offset: int,
) -> int:
    """
    Return the end of the word a label match stopped inside.

    Czech declines its role labels, so "obhospodarovatel" matches inside
    "Obhospodarovatelem". Without skipping the ending, the company name
    is read as "em Spolecnosti je CODYA investicni spolecnost".
    """

    end = offset

    while end < len(folded) and folded[end].isalpha():
        end += 1

    return end


def _first_label_span(
    *,
    folded: str,
    labels: Sequence[str],
) -> tuple[int, int] | None:
    """Return the span of the first matching label of a role."""

    for label in labels:
        span = find_label(
            folded=folded,
            label=label,
        )

        if span is not None:
            return span

    return None


def _company_after_label(
    *,
    block: str,
    start: int,
) -> str | None:
    """Return the company named right after a role label."""

    window = block[start : start + PARTY_LABEL_MAXIMUM_DISTANCE]

    for pattern in (
        MANAGEMENT_COMPANY_PATTERN,
        COMPANY_NAME_PATTERN,
    ):
        match = pattern.search(window)

        if match is None:
            continue

        name = clean_party_name(match.group("name"))

        if name is not None:
            return name

    return None


def _names_the_fund_itself(
    *,
    name: str,
    fund_tokens: tuple[str, ...],
) -> bool:
    """Return whether a captured company name is the fund itself."""

    if not fund_tokens:
        return False

    normalized = normalize_search_text(name)

    return all(token in normalized for token in fund_tokens)


def _build_party(
    *,
    role: PartyRole,
    name: str,
    block: str,
) -> FundParty | None:
    ico_match = ICO_PATTERN.search(block)

    ico = normalize_ico(ico_match.group("ico")) if ico_match is not None else None

    try:
        return FundParty(
            role=role,
            name=name,
            legal_name=name,
            ico=ico,
        )
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# 2. Capital observations and the assets history
# ---------------------------------------------------------------------------


def extract_aum_history(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[AumHistory]:
    """Extract every dated fund-level capital figure of one fund."""

    fund_level = [
        candidate
        for candidate in collect_capital_candidates(documents)
        if candidate.observation.metric_type not in NON_FUND_CAPITAL_METRICS
    ]

    if not fund_level:
        return missing_result(
            documents=documents,
            detail=(
                "No dated fund-level capital figure was found. Registered "
                "capital, the statutory minimum and manager-level assets "
                "were not used as a substitute."
            ),
        )

    return _series_result(
        fund_name=fund_name,
        candidates=_best_per_capital_key(fund_level),
        documents=documents,
        build=lambda accepted: AumHistory(
            observations=[candidate.observation for candidate in accepted],
        ),
        validate=lambda value: validate_capital_observations(value.observations),
        unconfirmed_detail=(
            "Dated fund-level capital figures were found, but none of "
            "them could be tied to this exact fund."
        ),
    )


# What a value read from a table is worth above the same value read from
# the flattened text. A structured candidate carries its own label and
# period, so it outranks a text candidate of the same figure.
TABLE_SCORE_BONUS: Final = 30


def _table_offset(
    *,
    document: ExtractionDocument,
    row_text: str,
) -> int:
    """
    Return where a table row sits inside the normalized document.

    The scope rules locate a value by its offset, so a table value needs
    a real one. When the row cannot be found the value is placed at the
    start, which is the most conservative position on a shared page.
    """

    normalized_document, _ = normalized_document_lines(document)

    needle = normalize_search_text(row_text)[:60]

    if not needle:
        return 0

    found = normalized_document.find(needle)

    return max(found, 0)


def _capital_from_tables(
    document: ExtractionDocument,
) -> list[CapitalCandidate]:
    """Read the dated capital figures out of the tables of one document."""

    candidates: list[CapitalCandidate] = []

    for page_number, table in document.document.iter_tables():
        for value in iter_table_values(table):
            if value.is_percentage or value.as_of is None or value.currency is None:
                # Without a period and a currency of its own the figure is
                # no better than what the text extractor already reads.
                continue

            normalized_label = normalize_search_text(value.label)

            if describes_value_per_share(normalized_label):
                # A per-share figure belongs to the value series, never
                # to the capital of the fund.
                continue

            metric = classify_capital_metric(normalized_label)

            if metric is None:
                continue

            metric = _refine_capital_metric(
                metric=metric,
                normalized_line=normalize_search_text(value.label),
                normalized_context=normalize_search_text(value.row_text),
            )

            try:
                observation = CapitalObservation(
                    amount=value.value,
                    currency=value.currency,
                    metric_type=metric,
                    as_of=value.as_of,
                    share_class=share_class_code(value.label),
                )
            except (ValidationError, ValueError):
                continue

            candidates.append(
                CapitalCandidate(
                    quote=value.row_text,
                    page_number=page_number,
                    document=document,
                    offset=_table_offset(
                        document=document,
                        row_text=value.row_text,
                    ),
                    score=(
                        document_priority(
                            document,
                            CAPITAL_DOCUMENT_PRIORITY,
                        )
                        + 70
                        + TABLE_SCORE_BONUS
                        + min(
                            max(value.as_of.year - 2000, 0),
                            40,
                        )
                    ),
                    observation=observation,
                )
            )

    return candidates


def _historical_from_tables(
    document: ExtractionDocument,
) -> list[HistoricalCandidate]:
    """Read the dated value series out of the tables of one document."""

    candidates: list[HistoricalCandidate] = []

    for page_number, table in document.document.iter_tables():
        for value in iter_table_values(table):
            if value.is_percentage or value.as_of is None or value.currency is None:
                continue

            normalized_label = normalize_search_text(value.label)

            value_type = classify_historical_value(normalized_label)

            if value_type is None:
                continue

            if describes_value_per_share(normalized_label):
                # "Fondovy kapital na 1 akcii" measures one share, so the
                # series it belongs to is the value of a share.
                value_type = HistoricalValueType.NAV_PER_SHARE

            if not _plausible_for_type(
                value_type=value_type,
                amount=value.value,
            ):
                continue

            try:
                observation = HistoricalValueObservation(
                    as_of=value.as_of,
                    value=value.value,
                    currency=value.currency,
                )
            except ValidationError:
                continue

            candidates.append(
                HistoricalCandidate(
                    quote=value.row_text,
                    page_number=page_number,
                    document=document,
                    offset=_table_offset(
                        document=document,
                        row_text=value.row_text,
                    ),
                    score=(
                        document_priority(
                            document,
                            CAPITAL_DOCUMENT_PRIORITY,
                        )
                        + 70
                        + TABLE_SCORE_BONUS
                    ),
                    value_type=value_type,
                    share_class=share_class_code(value.label),
                    currency=value.currency,
                    observation=observation,
                    context=normalize_search_text(value.row_text),
                )
            )

    return candidates


def _returns_from_tables(
    document: ExtractionDocument,
) -> list[ReturnCandidate]:
    """
    Read calendar-year performance out of a table with a year header.

    A performance table writes the years across the top and the result of
    each year underneath. Reading it as a grid keeps every result with
    its own year instead of pairing whichever two numbers stand closest.
    """

    candidates: list[ReturnCandidate] = []

    for page_number, table in document.document.iter_tables():
        for value in iter_table_values(table):
            if not value.is_percentage or value.year is None:
                continue

            if abs(value.value) > ANNUAL_RETURN_LIMIT_PERCENT:
                continue

            series_type = classify_return_series(
                normalize_search_text(f"{value.label} {value.row_text}")
            )

            if series_type is None:
                continue

            try:
                observation = AnnualReturnObservation(
                    year=value.year,
                    return_percent=value.value,
                    series_type=series_type,
                    share_class=share_class_code(value.label),
                )
            except ValidationError:
                continue

            candidates.append(
                ReturnCandidate(
                    quote=value.row_text,
                    page_number=page_number,
                    document=document,
                    offset=_table_offset(
                        document=document,
                        row_text=value.row_text,
                    ),
                    score=(
                        document_priority(
                            document,
                            RETURN_DOCUMENT_PRIORITY,
                        )
                        + 70
                        + TABLE_SCORE_BONUS
                        + (10 if series_type is ReturnSeriesType.CALENDAR_YEAR else 0)
                    ),
                    observation=observation,
                )
            )

    return candidates


def collect_capital_candidates(
    documents: list[ExtractionDocument],
) -> list[CapitalCandidate]:
    """
    Collect every dated capital figure of the parsed documents.

    Registered capital, the statutory minimum and the assets of the
    manager are kept with their own metric instead of being dropped, so
    that a later audit can prove which figure was refused and why.
    """

    candidates: list[CapitalCandidate] = []

    for extraction_document in documents:
        # A figure read out of a table keeps the label of its own row and
        # the date of its own column, which no amount of window widening
        # can recover once the grid has been flattened into a paragraph.
        candidates.extend(_capital_from_tables(extraction_document))

        _, document_lines = normalized_document_lines(extraction_document)

        for index, (line, page_number, offset) in enumerate(document_lines):
            context = "\n".join(
                item for item, _, _ in document_lines[max(0, index - 1) : index + 2]
            )

            normalized_line = normalize_search_text(line)

            normalized_context = normalize_search_text(context)

            metric = classify_capital_metric(normalized_line)

            if metric is None:
                continue

            if not _reads_as_a_row(
                normalized_line=normalized_line,
                maximum_words=MAXIMUM_CAPITAL_SENTENCE_WORDS,
            ):
                continue

            metric = _refine_capital_metric(
                metric=metric,
                normalized_line=normalized_line,
                normalized_context=normalized_context,
            )

            money_match = _money_after_metric(normalized=normalized_line)

            if money_match is None:
                continue

            as_of = extract_date(normalized_line) or extract_date(normalized_context)

            if as_of is None:
                continue

            amount = parse_money_amount(money_match) * _declared_only(
                money_match=money_match,
                normalized_line=normalized_line,
                normalized_context=normalized_context,
            )

            try:
                observation = CapitalObservation(
                    amount=amount,
                    currency=normalize_currency(money_match.group("currency")),
                    metric_type=metric,
                    as_of=as_of,
                    share_class=share_class_code(line),
                )
            except (ValidationError, ValueError):
                continue

            candidates.append(
                CapitalCandidate(
                    quote=context,
                    page_number=page_number,
                    document=extraction_document,
                    offset=offset,
                    score=(
                        document_priority(
                            extraction_document,
                            CAPITAL_DOCUMENT_PRIORITY,
                        )
                        + 70
                        + min(
                            max(as_of.year - 2000, 0),
                            40,
                        )
                    ),
                    observation=observation,
                )
            )

    return candidates


def _refine_capital_metric(
    *,
    metric: AumMetricType,
    normalized_line: str,
    normalized_context: str,
) -> AumMetricType:
    """
    Correct a metric read from its label alone.

    The label of a capital figure does not say who holds it. Only the
    sentence around it separates the assets of this fund from the assets
    of the whole group and from the floor the law requires.
    """

    if describes_manager_level_capital(normalized_line) or describes_manager_level_capital(
        normalized_context
    ):
        return AumMetricType.MANAGER_AUM

    if metric in {
        AumMetricType.STATUTORY_MINIMUM_CAPITAL,
        AumMetricType.REGISTERED_CAPITAL,
    }:
        return metric

    if describes_statutory_capital(normalized_line):
        return AumMetricType.STATUTORY_MINIMUM_CAPITAL

    if "podfond" in normalized_line and metric in {
        AumMetricType.FUND_CAPITAL,
        AumMetricType.ASSETS_UNDER_MANAGEMENT,
        AumMetricType.FUND_AUM,
    }:
        return AumMetricType.SUBFUND_AUM

    return metric


def _money_after_metric(
    *,
    normalized: str,
) -> re.Match[str] | None:
    """Return the amount belonging to the capital label of a line."""

    best: re.Match[str] | None = None

    for label, _ in CAPITAL_METRIC_BY_LABEL:
        start = normalized.find(label)

        while start >= 0:
            label_end = start + len(label)

            match = MONEY_PATTERN.search(
                normalized,
                label_end,
                label_end + AUM_LABEL_MAXIMUM_DISTANCE,
            )

            if match is not None and (best is None or match.start() < best.start()):
                best = match

            start = normalized.find(
                label,
                start + 1,
            )

    return best


def _declared_only(
    *,
    money_match: re.Match[str],
    normalized_line: str,
    normalized_context: str,
) -> float:
    """
    Return the unit a table declares for amounts that carry none.

    A Czech financial statement writes "(v tis. Kc)" once in its header
    and prints bare numbers underneath. An amount that states its own
    unit is already complete and must not be scaled twice.
    """

    if money_match.group("multiplier") is not None:
        return 1.0

    declared = declared_multiplier(normalized_line) or declared_multiplier(normalized_context)

    return declared or 1.0


def _best_per_capital_key(
    candidates: list[CapitalCandidate],
) -> list[CapitalCandidate]:
    """
    Keep one observation per metric, date, class and currency.

    A financial statement repeats the same figure in its summary and in
    its notes, and a duplicate date is one of the defects the extended
    output is validated against.
    """

    best: dict[
        tuple[str, str, str, str],
        CapitalCandidate,
    ] = {}

    for candidate in candidates:
        observation = candidate.observation

        key = (
            observation.metric_type.value,
            observation.as_of.isoformat(),
            observation.share_class or "",
            observation.currency,
        )

        current = best.get(key)

        if current is None or candidate.score > current.score:
            best[key] = candidate

    return sorted(
        best.values(),
        key=lambda item: (
            item.observation.as_of,
            item.observation.metric_type.value,
        ),
    )[:MAXIMUM_SERIES_OBSERVATIONS]


# ---------------------------------------------------------------------------
# 3. Annual returns
# ---------------------------------------------------------------------------


def extract_annual_returns(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[AnnualReturnHistory]:
    """Extract the calendar-year performance of one fund."""

    calendar = [
        candidate
        for candidate in collect_return_candidates(documents)
        if candidate.observation.series_type is ReturnSeriesType.CALENDAR_YEAR
    ]

    if not calendar:
        return missing_result(
            documents=documents,
            detail=(
                "No calendar-year performance was found. Cumulative "
                "returns and the performance scenarios of a KID were not "
                "used as a substitute."
            ),
        )

    return _series_result(
        fund_name=fund_name,
        candidates=_best_per_return_key(calendar),
        documents=documents,
        build=lambda accepted: AnnualReturnHistory(
            observations=[candidate.observation for candidate in accepted],
        ),
        validate=lambda value: validate_annual_returns(value.observations),
        unconfirmed_detail=(
            "Calendar-year performance figures were found, but none of "
            "them could be tied to this exact fund."
        ),
    )


def collect_return_candidates(
    documents: list[ExtractionDocument],
) -> list[ReturnCandidate]:
    """Collect every reported performance, with the period it covers."""

    candidates: list[ReturnCandidate] = []

    for extraction_document in documents:
        candidates.extend(_returns_from_tables(extraction_document))

        _, document_lines = normalized_document_lines(extraction_document)

        heading_series: ReturnSeriesType | None = None

        heading_index = 0

        for index, (line, page_number, offset) in enumerate(document_lines):
            normalized_line = normalize_search_text(line)

            context = "\n".join(
                item for item, _, _ in document_lines[max(0, index - 2) : index + 2]
            )

            line_series = classify_return_series(normalized_line)

            if line_series is not None:
                heading_series = line_series

                heading_index = index

            # A performance table names its period once, in the heading
            # above the rows. Reading only the row would classify every
            # cumulative or scenario figure as a calendar-year return.
            series_type = line_series or (
                heading_series if index - heading_index <= HEADING_MEMORY_LINES else None
            )

            if series_type is None:
                continue

            if not _reads_as_a_row(
                normalized_line=normalized_line,
                maximum_words=MAXIMUM_ROW_WORDS,
            ):
                continue

            if series_type is ReturnSeriesType.CALENDAR_YEAR and not _states_a_result(
                normalized_line
            ):
                continue

            share_class = share_class_code(line)

            for year, percent in _year_percent_pairs(line):
                try:
                    observation = AnnualReturnObservation(
                        year=year,
                        return_percent=percent,
                        series_type=series_type,
                        share_class=share_class,
                    )
                except ValidationError:
                    continue

                candidates.append(
                    ReturnCandidate(
                        quote=context,
                        page_number=page_number,
                        document=extraction_document,
                        offset=offset,
                        score=(
                            document_priority(
                                extraction_document,
                                RETURN_DOCUMENT_PRIORITY,
                            )
                            + 70
                            + (10 if series_type is ReturnSeriesType.CALENDAR_YEAR else 0)
                        ),
                        observation=observation,
                    )
                )

    return candidates


def _reads_as_a_row(
    *,
    normalized_line: str,
    maximum_words: int,
) -> bool:
    """Return whether a line is short enough to be a table row."""

    return len(normalized_line.split()) <= maximum_words


def _states_a_result(
    normalized_line: str,
) -> bool:
    """
    Return whether a line reports an achieved result.

    A calendar-year return has to be labelled as a performance on the row
    itself. A bare year and percentage of a narrative paragraph is a
    forecast, an interest rate or an inflation figure.
    """

    if any(marker in normalized_line for marker in PAST_RETURN_MARKERS):
        return True

    return len(normalized_line.split()) <= MAXIMUM_TABLE_ROW_WORDS


def _year_percent_pairs(
    line: str,
) -> list[tuple[int, float]]:
    """
    Return the year and performance pairs stated on one line.

    A performance table writes them next to each other, in either order.
    Requiring them to stand close together keeps the year of a footnote
    from being paired with a percentage of the row above it.
    """

    years = [
        (match.start(), int(match.group("year")))
        for match in YEAR_PATTERN.finditer(line)
        if EARLIEST_REPORTING_YEAR <= int(match.group("year")) <= LATEST_REPORTING_YEAR
    ]

    if not years:
        return []

    percents = list(_signed_percentages(line))

    pairs: list[tuple[int, float]] = []

    used: set[int] = set()

    for year_offset, year in years:
        best: tuple[int, int, float] | None = None

        for percent_offset, percent in percents:
            if percent_offset in used:
                continue

            distance = abs(percent_offset - year_offset)

            if distance > RETURN_YEAR_MAXIMUM_DISTANCE:
                continue

            if best is None or distance < best[0]:
                best = (
                    distance,
                    percent_offset,
                    percent,
                )

        if best is None:
            continue

        used.add(best[1])

        pairs.append(
            (
                year,
                best[2],
            )
        )

    return pairs


def _signed_percentages(
    line: str,
) -> Iterator[tuple[int, float]]:
    for match in SIGNED_PERCENT_PATTERN.finditer(line):
        raw_value = (
            match.group("value")
            .replace(
                "−",
                "-",
            )
            .replace(
                " ",
                "",
            )
        )

        negative = raw_value.startswith("-")

        try:
            value = parse_number(raw_value.lstrip("+-"))
        except ValueError:
            continue

        if negative:
            value = -value

        if abs(value) > ANNUAL_RETURN_LIMIT_PERCENT:
            continue

        yield (
            match.start(),
            value,
        )


def _best_per_return_key(
    candidates: list[ReturnCandidate],
) -> list[ReturnCandidate]:
    best: dict[
        tuple[int, str, str],
        ReturnCandidate,
    ] = {}

    for candidate in candidates:
        observation = candidate.observation

        key = (
            observation.year,
            observation.series_type.value,
            observation.share_class or "",
        )

        current = best.get(key)

        if current is None or candidate.score > current.score:
            best[key] = candidate

    return sorted(
        best.values(),
        key=lambda item: item.observation.year,
    )[:MAXIMUM_SERIES_OBSERVATIONS]


# ---------------------------------------------------------------------------
# 4. Historical values
# ---------------------------------------------------------------------------


def extract_historical_values(
    *,
    fund_name: str,
    documents: list[ExtractionDocument],
) -> FieldResult[HistoricalValueCollection]:
    """Extract the dated value series of one fund."""

    candidates = collect_historical_candidates(documents)

    if not candidates:
        return missing_result(
            documents=documents,
            detail=(
                "No dated series of a net asset value, an investment "
                "share value or a fund capital was found."
            ),
        )

    return _series_result(
        fund_name=fund_name,
        candidates=_best_per_historical_key(candidates),
        documents=documents,
        build=_build_historical_collection,
        validate=lambda value: validate_historical_series(value.series),
        unconfirmed_detail=(
            "Dated values were found, but none of them could be tied to this exact fund."
        ),
    )


def collect_historical_candidates(
    documents: list[ExtractionDocument],
) -> list[HistoricalCandidate]:
    """Collect every dated value together with the quantity it measures."""

    candidates: list[HistoricalCandidate] = []

    for extraction_document in documents:
        candidates.extend(_historical_from_tables(extraction_document))

        _, document_lines = normalized_document_lines(extraction_document)

        heading_type: HistoricalValueType | None = None

        heading_index = 0

        for index, (line, page_number, offset) in enumerate(document_lines):
            normalized_line = normalize_search_text(line)

            context = "\n".join(
                item for item, _, _ in document_lines[max(0, index - 2) : index + 2]
            )

            line_type = classify_historical_value(normalized_line)

            if line_type is not None:
                heading_type = line_type

                heading_index = index

            value_type = line_type or (
                heading_type if index - heading_index <= HEADING_MEMORY_LINES else None
            )

            if value_type is None:
                continue

            if not _reads_as_a_row(
                normalized_line=normalized_line,
                maximum_words=MAXIMUM_ROW_WORDS,
            ):
                continue

            for observation_date, amount, currency in _dated_amounts(normalized_line):
                if not _plausible_for_type(
                    value_type=value_type,
                    amount=amount,
                ):
                    continue

                try:
                    observation = HistoricalValueObservation(
                        as_of=observation_date,
                        value=amount,
                        currency=currency,
                    )
                except ValidationError:
                    continue

                candidates.append(
                    HistoricalCandidate(
                        quote=context,
                        page_number=page_number,
                        document=extraction_document,
                        offset=offset,
                        score=(
                            document_priority(
                                extraction_document,
                                CAPITAL_DOCUMENT_PRIORITY,
                            )
                            + 70
                        ),
                        value_type=value_type,
                        share_class=share_class_code(line),
                        currency=currency,
                        observation=observation,
                        context=normalize_search_text(context),
                    )
                )

    return candidates


def _dated_amounts(
    normalized: str,
) -> list[tuple[date, float, str]]:
    """
    Return every date and amount stated on one line.

    A value series prints one date and one amount per row, so the date
    standing in front of an amount is the date of that amount.
    """

    dates = _line_dates(normalized)

    if not dates:
        return []

    results: list[tuple[date, float, str]] = []

    for money_match in MONEY_PATTERN.finditer(normalized):
        governing = [item for item in dates if item[0] < money_match.start()]

        if not governing:
            continue

        declared = (
            1.0
            if money_match.group("multiplier") is not None
            else declared_multiplier(normalized) or 1.0
        )

        try:
            currency = normalize_currency(money_match.group("currency"))
        except ValueError:
            continue

        results.append(
            (
                governing[-1][1],
                parse_money_amount(money_match) * declared,
                currency,
            )
        )

    return results


def _line_dates(
    normalized: str,
) -> list[tuple[int, date]]:
    """Return the offset and value of every date on a line."""

    found: list[tuple[int, date]] = []

    for pattern in (
        DATE_ISO_PATTERN,
        DATE_NUMERIC_PATTERN,
    ):
        for match in pattern.finditer(normalized):
            parsed = safe_date(
                year=int(match.group("year")),
                month=int(match.group("month")),
                day=int(match.group("day")),
            )

            if parsed is not None:
                found.append(
                    (
                        match.start(),
                        parsed,
                    )
                )

    return sorted(found)


def _plausible_for_type(
    *,
    value_type: HistoricalValueType,
    amount: float,
) -> bool:
    """Reject a value whose size contradicts the quantity it claims to be."""

    if value_type in {
        HistoricalValueType.NAV_PER_SHARE,
        HistoricalValueType.INVESTMENT_SHARE_VALUE,
    }:
        return 0 < amount <= SHARE_VALUE_MAXIMUM

    return amount > 0


def _best_per_historical_key(
    candidates: list[HistoricalCandidate],
) -> list[HistoricalCandidate]:
    best: dict[
        tuple[str, str, str, str],
        HistoricalCandidate,
    ] = {}

    for candidate in candidates:
        key = (
            candidate.value_type.value,
            candidate.share_class or "",
            candidate.currency,
            candidate.observation.as_of.isoformat(),
        )

        current = best.get(key)

        if current is None or candidate.score > current.score:
            best[key] = candidate

    return sorted(
        best.values(),
        key=lambda item: (
            item.value_type.value,
            item.share_class or "",
            item.currency,
            item.observation.as_of,
        ),
    )[:MAXIMUM_SERIES_OBSERVATIONS]


def _build_historical_collection(
    accepted: Sequence[HistoricalCandidate],
) -> HistoricalValueCollection:
    """
    Build one series per quantity, share class and currency.

    The identity of a series lives on the series itself, so two metrics
    or two share classes can never share one list of observations.
    """

    grouped: dict[
        tuple[str, str, str],
        list[HistoricalCandidate],
    ] = {}

    for candidate in accepted:
        key = (
            candidate.value_type.value,
            candidate.share_class or "",
            candidate.currency,
        )

        grouped.setdefault(
            key,
            [],
        ).append(candidate)

    series: list[HistoricalValueSeries] = []

    for (value_type, share_class, currency), items in sorted(grouped.items()):
        observations = sorted(
            (item.observation for item in items),
            key=lambda observation: observation.as_of,
        )

        series.append(
            HistoricalValueSeries(
                value_type=HistoricalValueType(value_type),
                currency=currency,
                frequency=_series_frequency(
                    items=items,
                    observations=observations,
                ),
                share_class=share_class or None,
                observations=observations,
            )
        )

    return HistoricalValueCollection(series=series)


def _series_frequency(
    *,
    items: Sequence[HistoricalCandidate],
    observations: Sequence[HistoricalValueObservation],
) -> SeriesFrequency:
    """Return the frequency of a series, stated or measured."""

    for item in items:
        declared = declared_frequency(item.context)

        if declared is not None:
            return declared

    if len(observations) < 2:
        return SeriesFrequency.IRREGULAR

    gaps = sorted(
        (observations[index + 1].as_of - observations[index].as_of).days
        for index in range(len(observations) - 1)
    )

    return frequency_from_gap_days(float(gaps[len(gaps) // 2]))


# ---------------------------------------------------------------------------
# 5. Fund news
# ---------------------------------------------------------------------------


NEWS_LINK_PATTERN: Final = re.compile(
    r"<a\b[^>]*?href\s*=\s*[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<text>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


NEWS_TAG_PATTERN: Final = re.compile(r"<[^>]+>")


NEWS_DATE_IN_URL_PATTERN: Final = re.compile(
    r"/(?P<year>20\d{2})/(?P<month>\d{1,2})(?:/(?P<day>\d{1,2}))?/",
)


def extract_fund_news(
    *,
    fund_name: str,
    fund_web: str | None,
    documents: list[ExtractionDocument],
) -> FieldResult[FundNewsCollection]:
    """
    Extract the news published about one fund.

    Only the official website of the fund and the website of its manager
    are read, which is what this step covers. A headline of a manager
    page is kept only when it names this fund, because such a page lists
    the news of every fund of the group.
    """

    fund_host = canonical_domain(fund_web) if fund_web else ""

    fund_tokens = fund_identity_tokens(fund_name)

    items: dict[str, FundNewsItem] = {}

    evidence: ExtractionDocument | None = None

    for extraction_document in documents:
        record = extraction_document.record

        if not is_news_path(urlsplit(record.url).path):
            continue

        body = _read_body(record.local_path)

        if body is None:
            continue

        for item in _news_items_of_page(
            body=body,
            page_url=record.url,
            fund_host=fund_host,
            fund_tokens=fund_tokens,
        ):
            if str(item.url) in items:
                continue

            items[str(item.url)] = item

            if evidence is None:
                evidence = extraction_document

    if not items or evidence is None:
        return missing_result(
            documents=documents,
            detail=(
                "No news item was found on the official website of the fund or of its manager."
            ),
        )

    ordered = sorted(
        items.values(),
        key=lambda item: (
            item.published_at or date.min,
            item.title,
        ),
        reverse=True,
    )[:MAXIMUM_NEWS_ITEMS]

    # A shared manager site publishes the news of every fund it runs. An
    # item that survives the extraction but cannot be tied to this fund
    # is dropped here rather than delivered under its name.
    findings = validate_news_items(
        items=ordered,
        fund_name=fund_name,
        fund_web=fund_web,
    )

    rejected_urls = {
        str(item.url)
        for finding in findings
        if finding.severity is ValidationSeverity.REJECT
        for item in ordered
        if str(item.url) in finding.detail
    }

    ordered = [item for item in ordered if str(item.url) not in rejected_urls]

    if not ordered:
        return missing_result(
            documents=documents,
            detail=(
                "News items were found on a shared manager website, but "
                "none of them names this fund."
            ),
        )

    record = evidence.record

    confidence = max(
        (item.relation_confidence for item in ordered),
        key=_CONFIDENCE_RANK.__getitem__,
    )

    return FieldResult[FundNewsCollection](
        status=FieldStatus.FOUND,
        value=FundNewsCollection(items=ordered),
        raw_value="\n".join(item.title for item in ordered),
        scope=DataScope(
            type=ScopeType.FUND,
            fund_name=fund_name,
        ),
        source=Evidence(
            source=SourceMetadata(
                url=HttpUrl(record.url),
                document_type=resolve_document_type(record.document_type),
                retrieved_at=source_datetime(record),
                title=record.title,
            ),
            quote="\n".join(f"{item.title} ({item.url})" for item in ordered),
        ),
        extraction=ExtractionMetadata(
            method=ExtractionMethod.HTML_SELECTOR,
            confidence=confidence,
            review_required=(confidence is Confidence.LOW or bool(findings)),
        ),
    )


_CONFIDENCE_RANK: Final[dict[Confidence, int]] = {
    Confidence.LOW: 0,
    Confidence.MEDIUM: 1,
    Confidence.HIGH: 2,
}


def _read_body(
    local_path: str | None,
) -> bytes | None:
    if not local_path:
        return None

    try:
        return Path(local_path).read_bytes()
    except OSError:
        return None


def _news_items_of_page(
    *,
    body: bytes,
    page_url: str,
    fund_host: str,
    fund_tokens: tuple[str, ...],
) -> Iterator[FundNewsItem]:
    """Return the articles a news listing links to."""

    html = decode_html_bytes(body)

    page_host = canonical_domain(page_url)

    base = urlsplit(page_url)

    for match in NEWS_LINK_PATTERN.finditer(html):
        # A headline is written with entities for its quotation marks and
        # its non-breaking spaces. Left encoded they would be stored as
        # part of the title and shown to a reader as markup.
        title = " ".join(
            unescape(
                NEWS_TAG_PATTERN.sub(
                    " ",
                    match.group("text"),
                )
            )
            .replace(
                " ",
                " ",
            )
            .split()
        )

        if not title or len(title) > NEWS_TITLE_MAXIMUM_CHARACTERS:
            continue

        if is_news_link_noise(normalize_search_text(title)):
            continue

        url = _absolute_url(
            href=match.group("href").strip(),
            base=base,
        )

        if url is None:
            continue

        parts = urlsplit(url)

        if not is_news_article_path(parts.path):
            continue

        item_host = canonical_domain(url)

        if item_host != page_host:
            # A link leaving the site of the page is not the news of this
            # fund, however news-like its address looks.
            continue

        source_type = (
            NewsSourceType.OFFICIAL_FUND
            if fund_host and item_host == fund_host
            else NewsSourceType.MANAGER
        )

        mentions_fund = _mentions_fund(
            text=f"{title} {parts.path}",
            fund_tokens=fund_tokens,
        )

        if source_type is NewsSourceType.MANAGER and not mentions_fund:
            # The page belongs to the manager and lists the news of every
            # fund it runs. Without the name of this fund the item cannot
            # be attributed to it.
            continue

        try:
            yield FundNewsItem(
                title=title,
                url=HttpUrl(url),
                source_domain=item_host,
                source_type=source_type,
                published_at=_news_date(
                    url=url,
                    title=title,
                ),
                relation_confidence=_news_confidence(
                    source_type=source_type,
                    mentions_fund=mentions_fund,
                ),
            )
        except ValidationError:
            continue


def _absolute_url(
    *,
    href: str,
    base: SplitResult,
) -> str | None:
    scheme = base.scheme

    netloc = base.netloc

    path = base.path

    if href.startswith(
        (
            "mailto:",
            "tel:",
            "javascript:",
            "#",
            "data:",
        )
    ):
        return None

    if href.startswith(
        (
            "http://",
            "https://",
        )
    ):
        return href

    if href.startswith("//"):
        return f"{scheme}:{href}"

    if href.startswith("/"):
        return f"{scheme}://{netloc}{href}"

    prefix = path.rsplit(
        "/",
        1,
    )[0]

    return f"{scheme}://{netloc}{prefix}/{href}"


def _mentions_fund(
    *,
    text: str,
    fund_tokens: tuple[str, ...],
) -> bool:
    if not fund_tokens:
        return False

    normalized = normalize_search_text(
        text.replace(
            "-",
            " ",
        )
    )

    return all(token in normalized for token in fund_tokens)


def _news_confidence(
    *,
    source_type: NewsSourceType,
    mentions_fund: bool,
) -> Confidence:
    if source_type is NewsSourceType.OFFICIAL_FUND:
        return Confidence.HIGH if mentions_fund else Confidence.MEDIUM

    return Confidence.MEDIUM if mentions_fund else Confidence.LOW


def _news_date(
    *,
    url: str,
    title: str,
) -> date | None:
    match = NEWS_DATE_IN_URL_PATTERN.search(url)

    if match is not None:
        parsed = safe_date(
            year=int(match.group("year")),
            month=int(match.group("month")),
            day=int(match.group("day") or 1),
        )

        if parsed is not None:
            return parsed

    return extract_date(normalize_search_text(title))


# ---------------------------------------------------------------------------
# Shared scope handling for the collection fields
# ---------------------------------------------------------------------------


def _series_result[CandidateT: SeriesCandidate, ValueT](
    *,
    fund_name: str,
    candidates: Sequence[CandidateT],
    documents: list[ExtractionDocument],
    build: Callable[
        [Sequence[CandidateT]],
        ValueT,
    ],
    validate: Callable[
        [ValueT],
        list[ValidationFinding],
    ],
    unconfirmed_detail: str,
) -> FieldResult[ValueT]:
    """
    Confirm the scope of every observation and assemble the field.

    Each observation runs through the same scope classification as a
    single value, so an observation taken from the section of another
    fund on a shared page never enters the series.
    """

    accepted: list[tuple[CandidateT, SourceScope]] = []

    for candidate in candidates:
        scope = classify_source_scope(
            fund_name=fund_name,
            source_url=candidate.document.record.url,
            source_title=candidate.document.record.title,
            document_text=candidate.document.document.full_text,
            quote=candidate.quote,
            value_offset=candidate.offset,
            document=candidate.document,
        )

        if scope in ACCEPTED_SCOPES:
            accepted.append(
                (
                    candidate,
                    scope,
                )
            )

    if not accepted:
        return _unconfirmed_series_result(
            candidates=candidates,
            documents=documents,
            detail=unconfirmed_detail,
        )

    best_candidate, best_scope = max(
        accepted,
        key=lambda item: (
            item[0].score,
            item[0].document.record.source_id,
        ),
    )

    try:
        value = build([candidate for candidate, _ in accepted])
    except ValidationError:
        return _unconfirmed_series_result(
            candidates=candidates,
            documents=documents,
            detail=(
                "Observations were found, but they could not be assembled into a consistent series."
            ),
        )

    findings = validate(value)

    if rejects(findings):
        # A series that mixes metrics, repeats a date or contradicts
        # itself is worse than no series at all: a reader cannot tell
        # which of its numbers is the one the fund published.
        return _rejected_series_result(
            candidates=candidates,
            documents=documents,
            findings=findings,
        )

    confidence = confidence_from_score(best_candidate.score)

    record = best_candidate.document.record

    review_required = confidence is Confidence.LOW or best_scope is SourceScope.SHARE_CLASS

    # A series read from the layout fallback carries the same doubt as
    # any other value taken from a rebuilt text: the rows were assembled
    # from a page that was re-flowed, not read off it. The rule is the
    # one applied to the delivered fields, kept here because this result
    # is built without going through them.
    if is_anydoc_parser(record.parser_name):
        confidence, review_required = fallback_extraction_metadata(
            confidence,
            placed_on_a_page=best_candidate.page_number is not None,
        )

    return FieldResult[ValueT](
        status=FieldStatus.FOUND,
        value=value,
        raw_value=best_candidate.quote,
        scope=DataScope(
            type=_SCOPE_TYPES[best_scope],
            fund_name=fund_name,
        ),
        source=Evidence(
            source=SourceMetadata(
                url=HttpUrl(record.url),
                document_type=resolve_document_type(record.document_type),
                retrieved_at=source_datetime(record),
                title=record.title,
            ),
            quote=best_candidate.quote,
            page=best_candidate.page_number,
        ),
        extraction=ExtractionMetadata(
            method=ExtractionMethod.TABLE,
            confidence=confidence,
            review_required=review_required,
        ),
    )


_SCOPE_TYPES: Final[dict[SourceScope, ScopeType]] = {
    SourceScope.EXACT_FUND: ScopeType.FUND,
    SourceScope.SUBFUND: ScopeType.SUBFUND,
    SourceScope.SHARE_CLASS: ScopeType.SHARE_CLASS,
    SourceScope.MANAGER: ScopeType.MANAGER,
    SourceScope.OTHER_FUND: ScopeType.UNKNOWN,
    SourceScope.GENERIC: ScopeType.UNKNOWN,
}


def _rejected_series_result[CandidateT: SeriesCandidate, ValueT](
    *,
    candidates: Sequence[CandidateT],
    documents: list[ExtractionDocument],
    findings: Sequence[ValidationFinding],
) -> FieldResult[ValueT]:
    """Report a series refused by a validation rule, with the rule named."""

    detail = " ".join(f"[{finding.code.value}] {finding.detail}" for finding in findings)

    return FieldResult[ValueT](
        status=FieldStatus.CONFLICTING,
        reason=MissingReason(
            code=ReasonCode.CONFLICTING_VALUES,
            detail=detail[:2000],
        ),
        attempted_sources=_series_attempts(
            candidates=candidates,
            documents=documents,
            outcome=ReasonCode.CONFLICTING_VALUES,
            detail=detail[:300],
        ),
    )


def _unconfirmed_series_result[CandidateT: SeriesCandidate, ValueT](
    *,
    candidates: Sequence[CandidateT],
    documents: list[ExtractionDocument],
    detail: str,
) -> FieldResult[ValueT]:
    return FieldResult[ValueT](
        status=FieldStatus.AMBIGUOUS,
        reason=MissingReason(
            code=ReasonCode.ENTITY_NOT_MATCHED,
            detail=detail,
        ),
        attempted_sources=_series_attempts(
            candidates=candidates,
            documents=documents,
            outcome=ReasonCode.ENTITY_NOT_MATCHED,
            detail=detail,
        ),
    )


def _series_attempts[CandidateT: SeriesCandidate](
    *,
    candidates: Sequence[CandidateT],
    documents: list[ExtractionDocument],
    outcome: ReasonCode,
    detail: str,
) -> list[SourceAttempt]:
    """Return one attempt per source that took part in a refused series."""

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
            detail=detail,
        )
        for document in documents
    ]
