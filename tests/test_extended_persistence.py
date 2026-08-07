from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from fundscraper.database import (
    initialize_database,
    register_funds,
)
from fundscraper.extended_extraction import ExtendedFundFields
from fundscraper.extended_persistence import (
    load_extended_fields,
    persist_extended_fields,
)
from fundscraper.models import FundInput
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
    ScopeType,
    SeriesFrequency,
    SourceMetadata,
)
from fundscraper.output_service import stable_fund_id

FUND = FundInput(
    name="Rezidento Alfa SICAV, a.s.",
    web="https://www.rezidentoalfa.cz",
)


def _evidence() -> Evidence:
    return Evidence(
        source=SourceMetadata(
            url="https://www.rezidentoalfa.cz/vyrocni-zprava.pdf",  # type: ignore[arg-type]
            document_type=DocumentType.ANNUAL_REPORT,
            retrieved_at=datetime(
                2026,
                7,
                23,
                12,
                tzinfo=UTC,
            ),
        ),
        quote="Fondový kapitál k 31. 12. 2024: 1 200 000 000 Kč",
        page=7,
    )


def _extraction() -> ExtractionMetadata:
    return ExtractionMetadata(
        method=ExtractionMethod.TABLE,
        confidence=Confidence.HIGH,
        review_required=False,
    )


def _found[ValueT](value: ValueT) -> FieldResult[ValueT]:
    return FieldResult[ValueT](
        status=FieldStatus.FOUND,
        value=value,
        raw_value="Fondový kapitál k 31. 12. 2024: 1 200 000 000 Kč",
        scope=DataScope(
            type=ScopeType.FUND,
            fund_name=FUND.name,
        ),
        source=_evidence(),
        extraction=_extraction(),
    )


def _missing[ValueT]() -> FieldResult[ValueT]:
    return FieldResult[ValueT](
        status=FieldStatus.NOT_FOUND,
        reason=MissingReason(
            code=ReasonCode.SOURCE_NOT_FOUND,
            detail="No source was available.",
        ),
    )


def _extended() -> ExtendedFundFields:
    return ExtendedFundFields(
        manager=_found(
            FundParty(
                role=PartyRole.MANAGER,
                name="CODYA investiční společnost, a.s.",
                legal_name="CODYA investiční společnost, a.s.",
                ico="06876897",
            )
        ),
        administrator=_missing(),
        aum_history=_found(
            AumHistory(
                observations=[
                    CapitalObservation(
                        amount=1_200_000_000,
                        currency="CZK",
                        metric_type=AumMetricType.FUND_CAPITAL,
                        as_of=date(2024, 12, 31),
                    ),
                    CapitalObservation(
                        amount=1_350_000_000,
                        currency="CZK",
                        metric_type=AumMetricType.FUND_CAPITAL,
                        as_of=date(2025, 12, 31),
                    ),
                ]
            )
        ),
        annual_returns=_found(
            AnnualReturnHistory(
                observations=[
                    AnnualReturnObservation(
                        year=2023,
                        return_percent=7.73,
                    ),
                    AnnualReturnObservation(
                        year=2024,
                        return_percent=6.51,
                        share_class="PIA",
                    ),
                ]
            )
        ),
        historical_values=_found(
            HistoricalValueCollection(
                series=[
                    HistoricalValueSeries(
                        value_type=HistoricalValueType.INVESTMENT_SHARE_VALUE,
                        currency="CZK",
                        frequency=SeriesFrequency.MONTHLY,
                        share_class="PIA",
                        observations=[
                            HistoricalValueObservation(
                                as_of=date(2024, 5, 31),
                                value=1.1949,
                                currency="CZK",
                            ),
                            HistoricalValueObservation(
                                as_of=date(2024, 6, 30),
                                value=1.2141,
                                currency="CZK",
                            ),
                        ],
                    )
                ]
            )
        ),
        news=_found(
            FundNewsCollection(
                items=[
                    FundNewsItem(
                        title="Výroční zpráva 2024",
                        url="https://www.rezidentoalfa.cz/aktuality/vz-2024",  # type: ignore[arg-type]
                        source_domain="rezidentoalfa.cz",
                        source_type=NewsSourceType.OFFICIAL_FUND,
                        published_at=date(2025, 4, 15),
                        relation_confidence=Confidence.HIGH,
                    )
                ]
            )
        ),
    )


def test_stores_and_reads_back_every_extended_field(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "processing.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [FUND],
    )

    summary = persist_extended_fields(
        database_path=database_path,
        fund_id=stable_fund_id(FUND),
        extended=_extended(),
    )

    assert summary.parties == 1

    assert summary.capital_observations == 2

    assert summary.annual_returns == 2

    assert summary.historical_values == 2

    assert summary.news_items == 1

    assert summary.total == 8

    stored = load_extended_fields(
        database_path=database_path,
        fund_id=stable_fund_id(FUND),
    )

    assert [party.name for party in stored.parties] == ["CODYA investiční společnost, a.s."]

    assert stored.parties[0].ico == "06876897"

    assert [item.amount for item in stored.capital_observations] == [
        1_200_000_000,
        1_350_000_000,
    ]

    assert [item.year for item in stored.annual_returns] == [
        2023,
        2024,
    ]

    assert stored.annual_returns[1].share_class == "PIA"

    assert len(stored.historical_values) == 1

    series = stored.historical_values[0]

    assert series.value_type is HistoricalValueType.INVESTMENT_SHARE_VALUE

    assert series.share_class == "PIA"

    assert series.frequency is SeriesFrequency.MONTHLY

    assert [observation.value for observation in series.observations] == [
        1.1949,
        1.2141,
    ]

    assert [item.title for item in stored.news] == ["Výroční zpráva 2024"]

    assert stored.news[0].published_at == date(2025, 4, 15)


def test_repeating_a_run_does_not_duplicate_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "processing.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [FUND],
    )

    for _ in range(2):
        persist_extended_fields(
            database_path=database_path,
            fund_id=stable_fund_id(FUND),
            extended=_extended(),
        )

    stored = load_extended_fields(
        database_path=database_path,
        fund_id=stable_fund_id(FUND),
    )

    assert len(stored.parties) == 1

    assert len(stored.capital_observations) == 2

    assert len(stored.annual_returns) == 2

    assert len(stored.news) == 1

    assert sum(len(series.observations) for series in stored.historical_values) == 2


def test_stores_nothing_for_a_field_that_was_not_confirmed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "processing.sqlite3"

    initialize_database(database_path)

    register_funds(
        database_path,
        [FUND],
    )

    empty = ExtendedFundFields(
        manager=_missing(),
        administrator=_missing(),
        aum_history=_missing(),
        annual_returns=_missing(),
        historical_values=_missing(),
        news=_missing(),
    )

    summary = persist_extended_fields(
        database_path=database_path,
        fund_id=stable_fund_id(FUND),
        extended=empty,
    )

    assert summary.total == 0

    stored = load_extended_fields(
        database_path=database_path,
        fund_id=stable_fund_id(FUND),
    )

    assert not stored.parties

    assert not stored.capital_observations

    assert not stored.annual_returns

    assert not stored.historical_values

    assert not stored.news
