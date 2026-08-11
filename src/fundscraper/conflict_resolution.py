"""
How two sources that disagree about one value are compared.

Before this module the extraction had two ways of dealing with a
disagreement: a whole field became ``conflicting`` and lost both values,
or the higher-scoring observation of a series silently replaced the other
one. Neither said why, and the losing candidate was gone.

The rules here answer one question deterministically: given two
candidates that make a claim about the same thing, is one of them clearly
stronger? The comparison walks a fixed ladder of factors, most important
first, and the first factor that separates them decides. When nothing
separates them the answer is that nothing does, and the caller keeps the
disagreement rather than picking a side.

Nothing here throws a candidate away. Whatever loses is written into the
conflict ledger with its value, its source, its quote, its page, its date
and the factor that decided against it, and the delivered field records
it among its attempted sources.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from enum import StrEnum
from typing import Final
from urllib.parse import unquote

from fundscraper.anydoc_parser import is_anydoc_parser
from fundscraper.document_dates import DocumentDates, extract_document_dates
from fundscraper.domain_candidates import (
    EXCLUDED_DOMAINS,
    distinctive_name_tokens,
)
from fundscraper.extended_validation import is_paginated_source
from fundscraper.html_discovery import normalize_search_text
from fundscraper.normalization import canonical_domain

# Hosts of Czech fund managers and administrators. A document on one of
# them is a legitimate source, but the host says nothing about which of
# the manager's funds the document describes, so it is never on its own
# a reason to believe the value belongs to this fund.
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


class SourceAuthority(StrEnum):
    """
    How close a source stands to the fund that publishes the value.

    The classification is only ever applied to a candidate whose entity
    was already confirmed by the scope rules. A domain on its own never
    proves that a document belongs to a fund, and this enum does not make
    it prove one: it ranks sources that have already been accepted.
    """

    OFFICIAL_FUND = "official_fund"
    FUND_SPECIFIC_MANAGER = "fund_specific_manager"
    GENERIC_MANAGER = "generic_manager"
    EXTERNAL_FALLBACK = "external_fallback"
    UNKNOWN = "unknown"


AUTHORITY_RANK: Final[dict[SourceAuthority, int]] = {
    SourceAuthority.OFFICIAL_FUND: 4,
    SourceAuthority.FUND_SPECIFIC_MANAGER: 3,
    SourceAuthority.GENERIC_MANAGER: 2,
    SourceAuthority.EXTERNAL_FALLBACK: 1,
    SourceAuthority.UNKNOWN: 0,
}


def classify_authority(
    *,
    source_url: str,
    source_title: str | None,
    fund_name: str,
    fund_web: str | None,
    names_the_fund: bool,
) -> SourceAuthority:
    """
    Rank one already-accepted source by how close it is to the fund.

    ``names_the_fund`` is the caller's verdict that the document itself
    identifies this fund. It is what separates a document about this fund
    on a manager site from one that merely shares the manager's host.
    """

    host = canonical_domain(source_url)

    if not host:
        return SourceAuthority.UNKNOWN

    fund_host = canonical_domain(fund_web) if fund_web else ""

    if fund_host and host == fund_host:
        return SourceAuthority.OFFICIAL_FUND

    if names_the_fund or _url_names_the_fund(
        source_url=source_url,
        source_title=source_title,
        fund_name=fund_name,
    ):
        return SourceAuthority.FUND_SPECIFIC_MANAGER

    for excluded in EXCLUDED_DOMAINS:
        if host == excluded or host.endswith(f".{excluded}"):
            return SourceAuthority.EXTERNAL_FALLBACK

    if any(fragment in host for fragment in MANAGER_HOST_FRAGMENTS):
        return SourceAuthority.GENERIC_MANAGER

    return SourceAuthority.GENERIC_MANAGER


def _url_names_the_fund(
    *,
    source_url: str,
    source_title: str | None,
    fund_name: str,
) -> bool:
    """Return whether the address or the title names this exact fund."""

    tokens = distinctive_name_tokens(fund_name)

    if not tokens:
        return False

    text = normalize_search_text(
        unquote(source_url).replace(
            "-",
            " ",
        )
        + " "
        + (source_title or "")
    )

    return all(token in text for token in tokens)


class DateKind(StrEnum):
    """
    Which date of a document was used to compare two candidates.

    Two dates are only comparable when they mean the same thing. The
    day a factsheet reports its net asset value and the day a statute
    came into force are both dates of a document and say nothing about
    each other, so only candidates carrying the same kind are compared.
    """

    AS_OF = "as_of"
    REPORTING_PERIOD_END = "reporting_period_end"
    EFFECTIVE_AT = "effective_at"
    PUBLISHED_AT = "published_at"
    UNKNOWN = "unknown"


# The order a document's dates are consulted in. The first one present
# becomes the information date of every candidate read from it.
DATE_PREFERENCE: Final[tuple[DateKind, ...]] = (
    DateKind.AS_OF,
    DateKind.REPORTING_PERIOD_END,
    DateKind.EFFECTIVE_AT,
    DateKind.PUBLISHED_AT,
)


@dataclass(frozen=True, slots=True)
class InformationDate:
    kind: DateKind
    value: date | None = None

    def comparable_with(
        self,
        other: InformationDate,
    ) -> bool:
        return (
            self.value is not None
            and other.value is not None
            and self.kind is other.kind
            and self.kind is not DateKind.UNKNOWN
        )


UNKNOWN_DATE: Final = InformationDate(kind=DateKind.UNKNOWN)


def information_date(
    dates: DocumentDates,
) -> InformationDate:
    """Return the date that says when a document's figures were true."""

    for kind in DATE_PREFERENCE:
        dated = getattr(
            dates,
            kind.value,
            None,
        )

        if dated is not None:
            return InformationDate(
                kind=kind,
                value=dated.value,
            )

    return UNKNOWN_DATE


class DateReader:
    """
    Reads the dates of a document once and remembers them.

    Date extraction scans the head and the tail of a document. A fund has
    dozens of documents and a conflict touches few of them, so the work
    is done on demand and kept for the rest of the fund.
    """

    def __init__(self) -> None:
        self._dates: dict[int, InformationDate] = {}

    def read(
        self,
        *,
        source_id: int,
        url: str,
        text: str,
    ) -> InformationDate:
        cached = self._dates.get(source_id)

        if cached is not None:
            return cached

        found = information_date(
            extract_document_dates(
                text=text,
                url=url,
            )
        )

        self._dates[source_id] = found

        return found


# ---------------------------------------------------------------------------
# Comparable facts of one candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    """Everything the comparison is allowed to look at."""

    display_value: str
    raw_value: str
    source_id: int
    source_url: str
    source_title: str | None
    document_type: str | None
    quote: str
    page: int | None
    scope: str
    scope_rank: int
    authority: SourceAuthority
    document_priority: int
    date: InformationDate
    parser_name: str
    score: int

    @property
    def is_layout_fallback(self) -> bool:
        return is_anydoc_parser(self.parser_name)

    @property
    def evidence_rank(self) -> int:
        """
        How completely a candidate can be checked against its source.

        A quote is the minimum. A page number on a paginated document is
        what lets a reader open it at the right place; a web page has no
        page and is not penalised for the one it cannot have.
        """

        if not self.quote.strip():
            return 0

        if not is_paginated_source(self.source_url):
            return 2

        return 2 if self.page is not None else 1


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


class ComparisonFactor(StrEnum):
    """One reason a candidate can be stronger than another."""

    PARSER_PROVENANCE = "parser_provenance"
    SOURCE_AUTHORITY = "source_authority"
    SCOPE_SPECIFICITY = "scope_specificity"
    DOCUMENT_TYPE = "document_type"
    INFORMATION_DATE = "information_date"
    EVIDENCE_COMPLETENESS = "evidence_completeness"
    EXTRACTION_SCORE = "extraction_score"


@dataclass(frozen=True, slots=True)
class RankingFactor:
    """What one factor said about two candidates."""

    factor: ComparisonFactor
    left: str
    right: str
    favours: str


# Two extraction scores this close describe the same quality of match.
# The existing conflict rule already treated a twenty-point gap as noise;
# a score is the weakest factor of the ladder and only decides when the
# gap is wider than that.
SCORE_DECISION_MARGIN: Final = 25


# Two readings of one document have the same source, the same scope and
# the same date, so only the quality of the match itself can separate
# them, and any difference in it is a real difference.
SAME_DOCUMENT_SCORE_MARGIN: Final = 1


class Preference(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    NEITHER = "neither"


@dataclass(frozen=True, slots=True)
class Comparison:
    preference: Preference
    decided_by: ComparisonFactor | None
    factors: tuple[RankingFactor, ...]


def compare_candidates(
    left: CandidateFacts,
    right: CandidateFacts,
    *,
    score_margin: int = SCORE_DECISION_MARGIN,
) -> Comparison:
    """
    Say which of two candidates is clearly stronger, or that neither is.

    The ladder is walked most important factor first and the first factor
    that separates them decides. A factor that cannot speak — two
    documents whose dates are not comparable, two scores within the noise
    band — says nothing and the next one is consulted.

    ``score_margin`` is how far apart two extraction scores have to be
    before the weakest factor of the ladder is allowed to decide. Two
    readings of one document are separated by nothing else, so their
    caller lowers it.
    """

    factors: list[RankingFactor] = []

    decided: Preference = Preference.NEITHER

    decided_by: ComparisonFactor | None = None

    for factor, reading in _readings(
        left,
        right,
        score_margin=score_margin,
    ):
        preference, left_text, right_text = reading

        factors.append(
            RankingFactor(
                factor=factor,
                left=left_text,
                right=right_text,
                favours=preference.value,
            )
        )

        if decided is Preference.NEITHER and preference is not Preference.NEITHER:
            decided = preference

            decided_by = factor

    return Comparison(
        preference=decided,
        decided_by=decided_by,
        factors=tuple(factors),
    )


def _readings(
    left: CandidateFacts,
    right: CandidateFacts,
    *,
    score_margin: int,
) -> list[
    tuple[
        ComparisonFactor,
        tuple[Preference, str, str],
    ]
]:
    return [
        (
            ComparisonFactor.PARSER_PROVENANCE,
            _parser_reading(
                left,
                right,
            ),
        ),
        (
            ComparisonFactor.SOURCE_AUTHORITY,
            _ranked(
                AUTHORITY_RANK[left.authority],
                AUTHORITY_RANK[right.authority],
                left.authority.value,
                right.authority.value,
            ),
        ),
        (
            ComparisonFactor.SCOPE_SPECIFICITY,
            _ranked(
                left.scope_rank,
                right.scope_rank,
                left.scope,
                right.scope,
            ),
        ),
        (
            ComparisonFactor.DOCUMENT_TYPE,
            _ranked(
                left.document_priority,
                right.document_priority,
                f"{left.document_type or 'unknown'} ({left.document_priority})",
                f"{right.document_type or 'unknown'} ({right.document_priority})",
            ),
        ),
        (
            ComparisonFactor.INFORMATION_DATE,
            _date_reading(
                left,
                right,
            ),
        ),
        (
            ComparisonFactor.EVIDENCE_COMPLETENESS,
            _ranked(
                left.evidence_rank,
                right.evidence_rank,
                _evidence_text(left),
                _evidence_text(right),
            ),
        ),
        (
            ComparisonFactor.EXTRACTION_SCORE,
            _score_reading(
                left,
                right,
                score_margin=score_margin,
            ),
        ),
    ]


def _ranked(
    left_rank: int,
    right_rank: int,
    left_text: str,
    right_text: str,
) -> tuple[Preference, str, str]:
    if left_rank > right_rank:
        preference = Preference.LEFT
    elif right_rank > left_rank:
        preference = Preference.RIGHT
    else:
        preference = Preference.NEITHER

    return (
        preference,
        left_text,
        right_text,
    )


def _parser_reading(
    left: CandidateFacts,
    right: CandidateFacts,
) -> tuple[Preference, str, str]:
    """
    Keep a value read from a rebuilt page from outranking a read one.

    The layout fallback puts words next to each other that were never
    adjacent on the page, which is how a construction progress became a
    guaranteed return. Such a value never wins against a value read by
    the primary parser from a source of at least the same standing.
    """

    left_text = "layout_fallback" if left.is_layout_fallback else "primary_parser"

    right_text = "layout_fallback" if right.is_layout_fallback else "primary_parser"

    if left.is_layout_fallback == right.is_layout_fallback:
        return (
            Preference.NEITHER,
            left_text,
            right_text,
        )

    primary, fallback, preference = (
        (left, right, Preference.LEFT)
        if right.is_layout_fallback
        else (right, left, Preference.RIGHT)
    )

    if (
        AUTHORITY_RANK[primary.authority] >= AUTHORITY_RANK[fallback.authority]
        and primary.scope_rank >= fallback.scope_rank
    ):
        return (
            preference,
            left_text,
            right_text,
        )

    return (
        Preference.NEITHER,
        left_text,
        right_text,
    )


def _date_reading(
    left: CandidateFacts,
    right: CandidateFacts,
) -> tuple[Preference, str, str]:
    left_text = _date_text(left.date)

    right_text = _date_text(right.date)

    if not left.date.comparable_with(right.date):
        return (
            Preference.NEITHER,
            left_text,
            right_text,
        )

    assert left.date.value is not None and right.date.value is not None

    if left.date.value > right.date.value:
        preference = Preference.LEFT
    elif right.date.value > left.date.value:
        preference = Preference.RIGHT
    else:
        preference = Preference.NEITHER

    return (
        preference,
        left_text,
        right_text,
    )


def _date_text(
    dated: InformationDate,
) -> str:
    if dated.value is None:
        return "unknown"

    return f"{dated.value.isoformat()} ({dated.kind.value})"


def _evidence_text(
    facts: CandidateFacts,
) -> str:
    parts = ["quote" if facts.quote.strip() else "no quote"]

    if is_paginated_source(facts.source_url):
        parts.append(f"page {facts.page}" if facts.page is not None else "no page")

    return ", ".join(parts)


def _score_reading(
    left: CandidateFacts,
    right: CandidateFacts,
    *,
    score_margin: int,
) -> tuple[Preference, str, str]:
    left_text = str(left.score)

    right_text = str(right.score)

    if abs(left.score - right.score) < score_margin:
        return (
            Preference.NEITHER,
            left_text,
            right_text,
        )

    preference = Preference.LEFT if left.score > right.score else Preference.RIGHT

    return (
        preference,
        left_text,
        right_text,
    )


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------


NUMERIC_TOLERANCE: Final = 1e-6


def number_key(
    value: float | None,
) -> float | None:
    """Return a number in the form two sources have to agree on."""

    if value is None:
        return None

    return round(
        value,
        6,
    )


def money_key(
    *,
    amount: float,
    currency: str,
) -> tuple[float, str]:
    """
    Return an amount together with the currency it is stated in.

    Two amounts written differently are the same value; two amounts in
    different currencies are not, and are never normalized into one.
    """

    return (
        round(
            amount,
            2,
        ),
        currency.upper(),
    )


def company_key(
    name: str,
) -> str:
    """
    Return a company name reduced to what identifies it.

    Documents of one manager write its legal form as "a.s.", "a. s." and
    "a.s". Comparing the text verbatim reports the same company twice.
    """

    return "".join(character for character in normalize_search_text(name) if character.isalnum())


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class ConflictOutcome(StrEnum):
    EQUIVALENT = "equivalent_after_normalization"
    AUTO_RESOLVED = "auto_resolved"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class ConflictCandidateView:
    """One candidate as the conflict report keeps it."""

    value: str
    raw_value: str
    source_url: str
    source_title: str | None
    document_type: str | None
    quote: str
    page: int | None
    date: str | None
    date_kind: str
    scope: str
    authority: str
    parser_name: str
    score: int

    @classmethod
    def of(
        cls,
        facts: CandidateFacts,
    ) -> ConflictCandidateView:
        return cls(
            value=facts.display_value,
            raw_value=facts.raw_value[:400],
            source_url=facts.source_url,
            source_title=facts.source_title,
            document_type=facts.document_type,
            quote=facts.quote[:400],
            page=facts.page,
            date=(facts.date.value.isoformat() if facts.date.value else None),
            date_kind=facts.date.kind.value,
            scope=facts.scope,
            authority=facts.authority.value,
            parser_name=facts.parser_name,
            score=facts.score,
        )


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """One group of candidates that claimed the same thing."""

    fund_name: str
    field: str
    semantic_key: str
    outcome: ConflictOutcome
    reason: str
    decided_by: str | None
    selected: ConflictCandidateView | None
    alternatives: tuple[ConflictCandidateView, ...]
    factors: tuple[RankingFactor, ...]


@dataclass
class ConflictLedger:
    """
    Everything the resolver looked at while extracting one run.

    The ledger is the internal model the report is written from. The
    delivered output is unchanged by it: a losing candidate reaches the
    delivered file as an attempted source, and everything else about it
    lives here.
    """

    fund_name: str = ""
    records: list[ConflictRecord] = dataclass_field(default_factory=list)
    dates: DateReader = dataclass_field(default_factory=DateReader)

    def record(
        self,
        record: ConflictRecord,
    ) -> None:
        self.records.append(record)

    def for_fund(
        self,
        fund_name: str,
    ) -> ConflictLedger:
        """Return this ledger bound to one fund, sharing its date cache."""

        return ConflictLedger(
            fund_name=fund_name,
            records=self.records,
            dates=self.dates,
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution[ItemT]:
    """What the resolver decided about one group of claims."""

    outcome: ConflictOutcome
    selected: ItemT | None
    alternatives: tuple[ItemT, ...]
    record: ConflictRecord


def resolve_group[ItemT](
    *,
    fund_name: str,
    field: str,
    semantic_key: str,
    items: Sequence[ItemT],
    facts_of: Callable[[ItemT], CandidateFacts],
    value_key: Callable[[ItemT], Hashable],
) -> Resolution[ItemT]:
    """
    Decide between candidates that claim the same thing.

    Three answers are possible. Every candidate says the same after
    normalization, and the best-evidenced one is kept without any
    disagreement having existed. Exactly one candidate is clearly
    stronger than every candidate that says something else, and it is
    preferred with the factor that decided named. Or no candidate beats
    all the others, and the caller is told so rather than handed a guess.
    """

    groups = _value_groups(
        items=items,
        facts_of=facts_of,
        value_key=value_key,
    )

    leaders = [group[0] for group in groups]

    if len(groups) == 1:
        leader = leaders[0]

        return Resolution(
            outcome=ConflictOutcome.EQUIVALENT,
            selected=leader,
            alternatives=tuple(groups[0][1:]),
            record=ConflictRecord(
                fund_name=fund_name,
                field=field,
                semantic_key=semantic_key,
                outcome=ConflictOutcome.EQUIVALENT,
                reason=(
                    f"{len(items)} candidates state the same value after "
                    "normalization, so there is nothing to decide."
                ),
                decided_by=None,
                selected=ConflictCandidateView.of(facts_of(leader)),
                alternatives=tuple(
                    ConflictCandidateView.of(facts_of(item)) for item in groups[0][1:]
                ),
                factors=(),
            ),
        )

    # Two readings of one document are not two sources disagreeing. The
    # ladder above them is identical by construction, so the reading that
    # matched better is the better reading, and the decision is recorded
    # rather than made silently as it used to be.
    one_document = len({facts_of(item).source_id for item in items}) == 1

    winning_index, decided_by, factors = _clear_winner(
        leaders=leaders,
        facts_of=facts_of,
        score_margin=(SAME_DOCUMENT_SCORE_MARGIN if one_document else SCORE_DECISION_MARGIN),
    )

    winner = leaders[winning_index] if winning_index is not None else None

    if winner is None:
        return Resolution(
            outcome=ConflictOutcome.UNRESOLVED,
            selected=None,
            alternatives=tuple(leaders),
            record=ConflictRecord(
                fund_name=fund_name,
                field=field,
                semantic_key=semantic_key,
                outcome=ConflictOutcome.UNRESOLVED,
                reason=(
                    (
                        f"{len(leaders)} readings of one document state "
                        "different values and match it equally well, so "
                        "neither may be preferred automatically."
                    )
                    if one_document
                    else (
                        f"{len(leaders)} sources state different values and "
                        "no ranking factor makes one of them clearly "
                        "stronger, so neither may be preferred automatically."
                    )
                ),
                decided_by=None,
                selected=None,
                alternatives=tuple(ConflictCandidateView.of(facts_of(item)) for item in leaders),
                factors=factors,
            ),
        )

    # The rivals are the strongest statement of each other value, not
    # every candidate that repeated one. A second reading of the same
    # figure confirms the winner and is not an alternative to it.
    alternatives = tuple(item for item in leaders if item is not winner)

    return Resolution(
        outcome=ConflictOutcome.AUTO_RESOLVED,
        selected=winner,
        alternatives=alternatives,
        record=ConflictRecord(
            fund_name=fund_name,
            field=field,
            semantic_key=semantic_key,
            outcome=ConflictOutcome.AUTO_RESOLVED,
            reason=(
                "The selected candidate is stronger than every source that "
                "states something else, decided by "
                f"{decided_by.value if decided_by else 'rank'}."
            ),
            decided_by=(decided_by.value if decided_by else None),
            selected=ConflictCandidateView.of(facts_of(winner)),
            alternatives=tuple(ConflictCandidateView.of(facts_of(item)) for item in alternatives),
            factors=factors,
        ),
    )


def _value_groups[ItemT](
    *,
    items: Sequence[ItemT],
    facts_of: Callable[[ItemT], CandidateFacts],
    value_key: Callable[[ItemT], Hashable],
) -> list[list[ItemT]]:
    """Group the candidates by the value they state, strongest first."""

    grouped: dict[Hashable, list[ItemT]] = {}

    for item in items:
        grouped.setdefault(
            value_key(item),
            [],
        ).append(item)

    groups = [
        sorted(
            group,
            key=lambda item: _strength(facts_of(item)),
            reverse=True,
        )
        for group in grouped.values()
    ]

    return sorted(
        groups,
        key=lambda group: _strength(facts_of(group[0])),
        reverse=True,
    )


def _clear_winner[ItemT](
    *,
    leaders: Sequence[ItemT],
    facts_of: Callable[[ItemT], CandidateFacts],
    score_margin: int,
) -> tuple[
    int | None,
    ComparisonFactor | None,
    tuple[RankingFactor, ...],
]:
    """
    Return the candidate that beats every other one, if there is one.

    A candidate wins only by beating all of its rivals. Beating the
    weakest of three while tying with the strongest is not a decision,
    and the caller has to keep the disagreement.
    """

    contest = compare_candidates(
        facts_of(leaders[0]),
        facts_of(leaders[1]),
        score_margin=score_margin,
    )

    for index, candidate in enumerate(leaders):
        comparisons = [
            compare_candidates(
                facts_of(candidate),
                facts_of(other),
                score_margin=score_margin,
            )
            for other_index, other in enumerate(leaders)
            if other_index != index
        ]

        if all(item.preference is Preference.LEFT for item in comparisons):
            return (
                index,
                comparisons[0].decided_by,
                comparisons[0].factors,
            )

    return (
        None,
        None,
        contest.factors,
    )


def _strength(
    facts: CandidateFacts,
) -> tuple[int, ...]:
    """Order candidates the way the ladder compares them."""

    return (
        0 if facts.is_layout_fallback else 1,
        AUTHORITY_RANK[facts.authority],
        facts.scope_rank,
        facts.document_priority,
        facts.evidence_rank,
        facts.score,
        -facts.source_id,
    )
