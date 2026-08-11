"""
How the resolver decides between sources that disagree.

Each test states one rule of the ladder and, where the wiring matters,
follows it through a real extraction so that the delivered field and its
attempted sources are checked too. What every one of them guards is the
same promise: the losing candidate is never gone, and a value is only
preferred when something concrete makes it stronger.
"""

from __future__ import annotations

from datetime import date

from fundscraper.conflict_resolution import (
    AUTHORITY_RANK,
    CandidateFacts,
    ComparisonFactor,
    ConflictLedger,
    ConflictOutcome,
    DateKind,
    InformationDate,
    Preference,
    SourceAuthority,
    classify_authority,
    company_key,
    compare_candidates,
    money_key,
    number_key,
    resolve_group,
)
from fundscraper.field_extraction import extract_fund_fields
from fundscraper.output_models import FieldStatus, ReasonCode
from tests.test_field_extraction import create_extraction_document

FUND_NAME = "Example Fund SICAV a.s."

FUND_WEB = "https://www.examplefund.cz/"


def facts(
    *,
    value: str,
    source_id: int = 1,
    url: str = "https://www.examplefund.cz/statut.pdf",
    document_type: str = "statute",
    authority: SourceAuthority = SourceAuthority.OFFICIAL_FUND,
    scope_rank: int = 3,
    document_priority: int = 100,
    dated: InformationDate | None = None,
    parser_name: str = "pymupdf",
    score: int = 150,
    page: int | None = 4,
    quote: str = "the source line",
) -> CandidateFacts:
    return CandidateFacts(
        display_value=value,
        raw_value=quote,
        source_id=source_id,
        source_url=url,
        source_title=None,
        document_type=document_type,
        quote=quote,
        page=page,
        scope="exact_fund",
        scope_rank=scope_rank,
        authority=authority,
        document_priority=document_priority,
        date=dated or InformationDate(kind=DateKind.UNKNOWN),
        parser_name=parser_name,
        score=score,
    )


def resolve(
    *items: CandidateFacts,
    field: str = "target_return",
) -> object:
    return resolve_group(
        fund_name=FUND_NAME,
        field=field,
        semantic_key=field,
        items=list(items),
        facts_of=lambda item: item,
        value_key=lambda item: item.display_value,
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_treats_differently_written_numbers_as_the_same_value() -> None:
    assert number_key(7.0000001) == number_key(7.0)

    assert money_key(
        amount=1_000_000.0,
        currency="czk",
    ) == money_key(
        amount=1_000_000.004,
        currency="CZK",
    )


def test_treats_differently_written_legal_forms_as_the_same_company() -> None:
    assert company_key("CODYA investiční společnost, a.s.") == company_key(
        "CODYA investicni spolecnost a. s."
    )


def test_never_normalizes_two_currencies_into_one() -> None:
    assert money_key(
        amount=125_000.0,
        currency="EUR",
    ) != money_key(
        amount=125_000.0,
        currency="CZK",
    )


def test_candidates_that_agree_after_normalization_are_not_a_conflict() -> None:
    resolution = resolve(
        facts(
            value="1000000.0",
            source_id=1,
        ),
        facts(
            value="1000000.0",
            source_id=2,
            url="https://www.examplefund.cz/kid.pdf",
        ),
    )

    assert resolution.outcome is ConflictOutcome.EQUIVALENT  # type: ignore[attr-defined]

    assert resolution.selected is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_the_official_fund_site_outranks_a_generic_manager_page() -> None:
    official = facts(
        value="7.0",
        source_id=1,
    )

    manager = facts(
        value="9.0",
        source_id=2,
        url="https://www.avantfunds.cz/fondy/prehled.pdf",
        authority=SourceAuthority.GENERIC_MANAGER,
    )

    comparison = compare_candidates(
        official,
        manager,
    )

    assert comparison.preference is Preference.LEFT

    assert comparison.decided_by is ComparisonFactor.SOURCE_AUTHORITY


def test_a_fund_specific_manager_document_outranks_a_generic_one() -> None:
    specific = classify_authority(
        source_url=("https://www.avantfunds.cz/fondy/example-fund-sicav/statut.pdf"),
        source_title=None,
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        names_the_fund=False,
    )

    generic = classify_authority(
        source_url="https://www.avantfunds.cz/dokumenty/cenik.pdf",
        source_title=None,
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        names_the_fund=False,
    )

    assert specific is SourceAuthority.FUND_SPECIFIC_MANAGER

    assert generic is SourceAuthority.GENERIC_MANAGER

    assert AUTHORITY_RANK[specific] > AUTHORITY_RANK[generic]


def test_an_exact_fund_scope_outranks_a_share_class_one() -> None:
    comparison = compare_candidates(
        facts(
            value="7.0",
            scope_rank=3,
        ),
        facts(
            value="9.0",
            source_id=2,
            scope_rank=1,
        ),
    )

    assert comparison.preference is Preference.LEFT

    assert comparison.decided_by is ComparisonFactor.SCOPE_SPECIFICITY


def test_the_newer_of_two_equally_strong_official_sources_wins() -> None:
    newer = facts(
        value="7.0",
        source_id=1,
        dated=InformationDate(
            kind=DateKind.EFFECTIVE_AT,
            value=date(2026, 1, 1),
        ),
    )

    older = facts(
        value="9.0",
        source_id=2,
        url="https://www.examplefund.cz/statut-2019.pdf",
        dated=InformationDate(
            kind=DateKind.EFFECTIVE_AT,
            value=date(2019, 1, 1),
        ),
    )

    comparison = compare_candidates(
        newer,
        older,
    )

    assert comparison.preference is Preference.LEFT

    assert comparison.decided_by is ComparisonFactor.INFORMATION_DATE

    resolution = resolve(
        newer,
        older,
    )

    assert resolution.outcome is ConflictOutcome.AUTO_RESOLVED  # type: ignore[attr-defined]

    assert resolution.record.decided_by == "information_date"  # type: ignore[attr-defined]


def test_dates_that_mean_different_things_do_not_decide() -> None:
    """A statute's effective day says nothing about a factsheet's as-of day."""

    comparison = compare_candidates(
        facts(
            value="7.0",
            dated=InformationDate(
                kind=DateKind.EFFECTIVE_AT,
                value=date(2019, 1, 1),
            ),
        ),
        facts(
            value="9.0",
            source_id=2,
            url="https://www.examplefund.cz/factsheet.pdf",
            dated=InformationDate(
                kind=DateKind.AS_OF,
                value=date(2026, 6, 30),
            ),
        ),
    )

    date_reading = next(
        item for item in comparison.factors if item.factor is ComparisonFactor.INFORMATION_DATE
    )

    assert date_reading.favours == Preference.NEITHER.value


def test_two_equally_strong_official_sources_stay_conflicting() -> None:
    resolution = resolve(
        facts(
            value="7.0",
            source_id=1,
        ),
        facts(
            value="9.0",
            source_id=2,
            url="https://www.examplefund.cz/statut-b.pdf",
        ),
    )

    assert resolution.outcome is ConflictOutcome.UNRESOLVED  # type: ignore[attr-defined]

    assert resolution.selected is None  # type: ignore[attr-defined]

    # Both are kept, so a reviewer sees what the two sources said.
    assert len(resolution.record.alternatives) == 2  # type: ignore[attr-defined]


def test_a_slightly_better_score_alone_never_decides() -> None:
    comparison = compare_candidates(
        facts(
            value="7.0",
            score=160,
        ),
        facts(
            value="9.0",
            source_id=2,
            url="https://www.examplefund.cz/statut-b.pdf",
            score=145,
        ),
    )

    assert comparison.preference is Preference.NEITHER


def test_the_layout_fallback_never_outranks_the_primary_parser() -> None:
    """
    The AnyDoc experiment turned a construction progress into a return.

    A value read from a rebuilt page loses to one read by the primary
    parser from a source of at least the same standing, whatever else
    the fallback candidate has going for it.
    """

    fallback = facts(
        value="75.0",
        source_id=1,
        score=200,
        document_priority=100,
        dated=InformationDate(
            kind=DateKind.EFFECTIVE_AT,
            value=date(2026, 1, 1),
        ),
        parser_name="anydoc+layout_fallback",
    )

    primary = facts(
        value="8.0",
        source_id=2,
        url="https://www.examplefund.cz/statut-b.pdf",
        score=120,
        document_priority=90,
        dated=InformationDate(
            kind=DateKind.EFFECTIVE_AT,
            value=date(2019, 1, 1),
        ),
    )

    comparison = compare_candidates(
        fallback,
        primary,
    )

    assert comparison.preference is Preference.RIGHT

    assert comparison.decided_by is ComparisonFactor.PARSER_PROVENANCE

    resolution = resolve(
        fallback,
        primary,
    )

    assert resolution.outcome is ConflictOutcome.AUTO_RESOLVED  # type: ignore[attr-defined]

    assert resolution.selected is primary  # type: ignore[attr-defined]


def test_a_candidate_that_only_beats_the_weakest_rival_decides_nothing() -> None:
    """Winning means beating everyone, not beating someone."""

    resolution = resolve(
        facts(
            value="7.0",
            source_id=1,
        ),
        facts(
            value="9.0",
            source_id=2,
            url="https://www.examplefund.cz/statut-b.pdf",
        ),
        facts(
            value="12.0",
            source_id=3,
            url="https://www.avantfunds.cz/prehled.pdf",
            authority=SourceAuthority.GENERIC_MANAGER,
        ),
    )

    assert resolution.outcome is ConflictOutcome.UNRESOLVED  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_a_resolved_conflict_keeps_the_losing_value_and_its_reason() -> None:
    statute = create_extraction_document(
        source_id=1,
        url="https://www.examplefund.cz/statut.pdf",
        title="Example Fund statut",
        text=("Example Fund SICAV a.s.\nMinimalni investice cini 1 000 000 Kc."),
        document_type="statute",
    )

    leaflet = create_extraction_document(
        source_id=2,
        url="https://www.examplefund.cz/letak.pdf",
        title="Example Fund letak",
        text=("Example Fund SICAV a.s.\nMinimalni investice cini 500 000 Kc."),
        document_type="marketing_page",
    )

    result = extract_fund_fields(
        fund_name=FUND_NAME,
        documents=[
            statute,
            leaflet,
        ],
        fund_web=FUND_WEB,
    )

    assert result.minimum_investment.status is FieldStatus.FOUND

    assert result.minimum_investment.value is not None

    assert result.minimum_investment.value.amount == 1_000_000.0

    losing = [
        attempt
        for attempt in result.minimum_investment.attempted_sources
        if attempt.outcome is ReasonCode.CONFLICTING_VALUES
    ]

    assert len(losing) == 1

    assert str(losing[0].url) == "https://www.examplefund.cz/letak.pdf"

    assert losing[0].detail is not None

    assert "500 000" in losing[0].detail


def test_different_dates_and_metrics_are_not_treated_as_conflicts() -> None:
    """
    Two capital figures of different days are both true.

    Before Step 8 the assets field had no conflict detection at all; now
    that it has one, it must not fire on figures that never contradicted
    each other.
    """

    first = create_extraction_document(
        source_id=1,
        url="https://www.examplefund.cz/vz-2024.pdf",
        title="Example Fund vyrocni zprava 2024",
        text=("Example Fund SICAV a.s.\nFondovy kapital k 31. 12. 2024 cini 530 168 000 Kc."),
        document_type="annual_report",
    )

    second = create_extraction_document(
        source_id=2,
        url="https://www.examplefund.cz/vz-2023.pdf",
        title="Example Fund vyrocni zprava 2023",
        text=("Example Fund SICAV a.s.\nFondovy kapital k 31. 12. 2023 cini 480 000 000 Kc."),
        document_type="annual_report",
    )

    ledger = ConflictLedger()

    result = extract_fund_fields(
        fund_name=FUND_NAME,
        documents=[
            first,
            second,
        ],
        fund_web=FUND_WEB,
        ledger=ledger,
    )

    assert result.assets_under_management.status is FieldStatus.FOUND

    unresolved = [
        record
        for record in ledger.records
        if record.field == "assets_under_management"
        and record.outcome is ConflictOutcome.UNRESOLVED
    ]

    assert unresolved == []


def test_an_annual_result_two_sources_disagree_about_is_left_out() -> None:
    """
    A contested year is dropped from the series and both readings kept.

    Refusing the whole history because one of its years is contested
    would lose more than it protects, so the point is left out, both
    sources are recorded, and the field asks for review.
    """

    first = create_extraction_document(
        source_id=1,
        url="https://www.examplefund.cz/vz-a.pdf",
        title="Example Fund vyrocni zprava",
        text=(
            "Example Fund SICAV a.s.\nVykonnost fondu v jednotlivych letech\n2023 6,2 %\n2024 7,1 %"
        ),
        document_type="annual_report",
    )

    second = create_extraction_document(
        source_id=2,
        url="https://www.examplefund.cz/vz-b.pdf",
        title="Example Fund vyrocni zprava",
        text=(
            "Example Fund SICAV a.s.\nVykonnost fondu v jednotlivych letech\n2023 9,8 %\n2024 7,1 %"
        ),
        document_type="annual_report",
    )

    from fundscraper.extended_extraction import extract_annual_returns

    ledger = ConflictLedger()

    result = extract_annual_returns(
        fund_name=FUND_NAME,
        documents=[
            first,
            second,
        ],
        fund_web=FUND_WEB,
        ledger=ledger,
    )

    contested = [
        record
        for record in ledger.records
        if record.outcome is ConflictOutcome.UNRESOLVED and record.semantic_key.startswith("2023")
    ]

    assert contested, "the contested year should be recorded"

    assert len(contested[0].alternatives) == 2

    assert result.status is FieldStatus.FOUND

    assert result.value is not None

    years = {observation.year for observation in result.value.observations}

    # The year the two reports disagree about is left out; the year they
    # agree on survives.
    assert years == {2024}

    assert result.extraction is not None

    assert result.extraction.review_required is True

    # Both readings of the contested year stay in the delivered field.
    assert {str(attempt.url) for attempt in result.attempted_sources} == {
        "https://www.examplefund.cz/vz-a.pdf",
        "https://www.examplefund.cz/vz-b.pdf",
    }
