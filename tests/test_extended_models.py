"""
Round-trip tests for the schema version 3 output models.

They check that the extended fields serialize and load unchanged, and
that an output written before those fields existed still loads.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl, ValidationError

from fundscraper.models import FundInput
from fundscraper.output_models import (
    FUND_LEVEL_AUM_METRICS,
    Annualization,
    AnnualReturnHistory,
    AnnualReturnObservation,
    AumHistory,
    AumMetricType,
    CapitalObservation,
    Confidence,
    DocumentType,
    Evidence,
    ExtractionMetadata,
    ExtractionMethod,
    FeeCollection,
    FeeItem,
    FeeTier,
    FeeTierBasis,
    FeeType,
    FieldResult,
    FieldStatus,
    FundNewsCollection,
    FundNewsItem,
    FundParty,
    HistoricalValueCollection,
    HistoricalValueObservation,
    HistoricalValueSeries,
    HistoricalValueType,
    HorizonKind,
    InferenceReference,
    InferenceType,
    InvestmentHorizonValue,
    MinimumInvestmentKind,
    MinimumInvestmentValue,
    NewsSourceType,
    PartyRole,
    ReturnSeriesType,
    ReturnType,
    SeriesFrequency,
    SourceMetadata,
    ValueOrigin,
)
from fundscraper.output_service import (
    OUTPUT_ADAPTER,
    create_pending_fund,
    load_output,
    write_output,
)

FUND = FundInput(
    name="EXAMPLE fond SICAV, a.s.",
    web="https://www.examplefond.cz/",
)


def evidence() -> Evidence:
    return Evidence(
        source=SourceMetadata(
            url=HttpUrl("https://www.examplefond.cz/vz-2025.pdf"),
            document_type=DocumentType.ANNUAL_REPORT,
            retrieved_at=datetime(2026, 1, 15, tzinfo=UTC),
        ),
        quote="Fondovy kapital: 693 601 745 Kc",
        page=12,
    )


def extraction() -> ExtractionMetadata:
    return ExtractionMetadata(
        method=ExtractionMethod.REGEX,
        confidence=Confidence.HIGH,
    )


def test_fund_level_metrics_exclude_capital_and_manager_assets() -> None:
    """Registered capital and manager assets are never fund assets."""

    for metric in (
        AumMetricType.REGISTERED_CAPITAL,
        AumMetricType.STATUTORY_MINIMUM_CAPITAL,
        AumMetricType.MANAGER_AUM,
        AumMetricType.OTHER,
    ):
        assert metric not in FUND_LEVEL_AUM_METRICS

    for metric in (
        AumMetricType.ASSETS_UNDER_MANAGEMENT,
        AumMetricType.FUND_AUM,
        AumMetricType.NET_ASSETS,
        AumMetricType.FUND_CAPITAL,
    ):
        assert metric in FUND_LEVEL_AUM_METRICS


def test_horizon_supports_exact_minimum_range_and_wording() -> None:
    exact = InvestmentHorizonValue(recommended_years=5)

    assert exact.kind is HorizonKind.EXACT

    spread = InvestmentHorizonValue(
        recommended_years=5,
        kind=HorizonKind.RANGE,
        minimum_years=5,
        maximum_years=7,
    )

    assert spread.maximum_years == 7

    textual = InvestmentHorizonValue(
        recommended_years=5,
        kind=HorizonKind.MINIMUM,
        minimum_years=5,
        wording="nejmene 5 let",
    )

    assert textual.wording == "nejmene 5 let"

    with pytest.raises(ValidationError):
        InvestmentHorizonValue(
            recommended_years=5,
            kind=HorizonKind.RANGE,
            minimum_years=7,
            maximum_years=5,
        )


def test_inferred_minimum_investment_requires_its_legal_basis() -> None:
    explicit = MinimumInvestmentValue(
        amount=1_000_000,
        currency="CZK",
        kind=MinimumInvestmentKind.INITIAL_SUBSCRIPTION,
    )

    assert explicit.origin is ValueOrigin.EXPLICIT

    inferred = MinimumInvestmentValue(
        amount=1_000_000,
        currency="CZK",
        kind=MinimumInvestmentKind.LEGAL_THRESHOLD,
        origin=ValueOrigin.INFERRED,
        inference=InferenceReference(
            inference_type=InferenceType.LEGAL_DEFAULT,
            legal_basis="ZISIF, paragraf 272",
            jurisdiction="CZ",
            effective_date=date(2013, 8, 19),
        ),
    )

    assert inferred.inference is not None

    with pytest.raises(ValidationError):
        MinimumInvestmentValue(
            amount=1_000_000,
            currency="CZK",
            kind=MinimumInvestmentKind.LEGAL_THRESHOLD,
            origin=ValueOrigin.INFERRED,
        )


def test_conditional_fee_keeps_every_tier() -> None:
    """An exit fee falling with the holding period is not one number."""

    item = FeeItem(
        type=FeeType.EXIT,
        maximum=True,
        details="10 % do 1 roku, 5 % do 2 let, 2,5 % do 3 let, 0 % po 3 letech",
        tiers=[
            FeeTier(
                basis=FeeTierBasis.HOLDING_PERIOD,
                from_months=0,
                to_months=12,
                rate_percent=10,
            ),
            FeeTier(
                basis=FeeTierBasis.HOLDING_PERIOD,
                from_months=12,
                to_months=24,
                rate_percent=5,
            ),
            FeeTier(
                basis=FeeTierBasis.HOLDING_PERIOD,
                from_months=24,
                to_months=36,
                rate_percent=2.5,
            ),
            FeeTier(
                basis=FeeTierBasis.HOLDING_PERIOD,
                from_months=36,
                rate_percent=0,
            ),
        ],
    )

    assert len(item.tiers) == 4
    assert [tier.rate_percent for tier in item.tiers] == [10, 5, 2.5, 0]

    # A fee described only by its tiers stays valid.
    assert item.rate_percent is None


def test_value_series_rejects_duplicate_dates_and_mixed_currency() -> None:
    observations = [
        HistoricalValueObservation(
            as_of=date(2025, 12, 31),
            value=1.2512,
            currency="CZK",
        ),
        HistoricalValueObservation(
            as_of=date(2024, 12, 31),
            value=1.1138,
            currency="CZK",
        ),
    ]

    series = HistoricalValueSeries(
        value_type=HistoricalValueType.NAV_PER_SHARE,
        currency="CZK",
        frequency=SeriesFrequency.ANNUAL,
        share_class="A",
        observations=observations,
    )

    assert len(series.observations) == 2

    with pytest.raises(ValidationError):
        HistoricalValueSeries(
            value_type=HistoricalValueType.NAV_PER_SHARE,
            currency="CZK",
            observations=[observations[0], observations[0]],
        )

    with pytest.raises(ValidationError):
        HistoricalValueSeries(
            value_type=HistoricalValueType.NAV_PER_SHARE,
            currency="EUR",
            observations=observations,
        )


def extended_fund() -> Any:
    fund = create_pending_fund(FUND)

    return fund.model_copy(
        update={
            "manager": FieldResult[FundParty](
                status=FieldStatus.FOUND,
                value=FundParty(
                    role=PartyRole.MANAGER,
                    name="AMISTA investicni spolecnost, a.s.",
                    ico="27437558",
                ),
                raw_value="AMISTA investicni spolecnost, a.s.",
                source=evidence(),
                extraction=extraction(),
            ),
            "administrator": FieldResult[FundParty](
                status=FieldStatus.FOUND,
                value=FundParty(
                    role=PartyRole.ADMINISTRATOR,
                    name="AVANT investicni spolecnost, a.s.",
                ),
                raw_value="AVANT investicni spolecnost, a.s.",
                source=evidence(),
                extraction=extraction(),
            ),
            "aum_history": FieldResult[AumHistory](
                status=FieldStatus.FOUND,
                value=AumHistory(
                    observations=[
                        CapitalObservation(
                            amount=693_601_745,
                            currency="CZK",
                            metric_type=AumMetricType.FUND_CAPITAL,
                            as_of=date(2025, 12, 31),
                            source=evidence(),
                            extraction=extraction(),
                        ),
                        CapitalObservation(
                            amount=2_000_000,
                            currency="CZK",
                            metric_type=AumMetricType.REGISTERED_CAPITAL,
                            as_of=date(2025, 12, 31),
                        ),
                    ]
                ),
                raw_value="Fondovy kapital: 693 601 745 Kc",
                source=evidence(),
                extraction=extraction(),
            ),
            "annual_returns": FieldResult[AnnualReturnHistory](
                status=FieldStatus.FOUND,
                value=AnnualReturnHistory(
                    observations=[
                        AnnualReturnObservation(
                            year=2024,
                            return_percent=7.2,
                            series_type=ReturnSeriesType.CALENDAR_YEAR,
                            share_class="A",
                        ),
                        AnnualReturnObservation(
                            year=2025,
                            return_percent=6.1,
                            series_type=ReturnSeriesType.CALENDAR_YEAR,
                            share_class="A",
                        ),
                    ]
                ),
                raw_value="2024: 7,2 %  2025: 6,1 %",
                source=evidence(),
                extraction=extraction(),
            ),
            "historical_values": FieldResult[HistoricalValueCollection](
                status=FieldStatus.FOUND,
                value=HistoricalValueCollection(
                    series=[
                        HistoricalValueSeries(
                            value_type=HistoricalValueType.NAV_PER_SHARE,
                            currency="CZK",
                            frequency=SeriesFrequency.ANNUAL,
                            share_class="A",
                            observations=[
                                HistoricalValueObservation(
                                    as_of=date(2025, 12, 31),
                                    value=1.2512,
                                    currency="CZK",
                                ),
                            ],
                        )
                    ]
                ),
                raw_value="1,2512 Kc",
                source=evidence(),
                extraction=extraction(),
            ),
            "news": FieldResult[FundNewsCollection](
                status=FieldStatus.FOUND,
                value=FundNewsCollection(
                    items=[
                        FundNewsItem(
                            title="Fond koupil novou nemovitost",
                            url=HttpUrl("https://www.examplefond.cz/aktuality/nova-nemovitost"),
                            source_domain="examplefond.cz",
                            source_type=NewsSourceType.OFFICIAL_FUND,
                            published_at=date(2026, 2, 1),
                            relation_confidence=Confidence.HIGH,
                        )
                    ]
                ),
                raw_value="Fond koupil novou nemovitost",
                source=evidence(),
                extraction=extraction(),
            ),
        }
    )


def test_extended_output_survives_a_json_round_trip(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "funds.json"

    original = extended_fund()

    write_output(
        output_path,
        [original],
        overwrite=True,
    )

    loaded = load_output(output_path)[0]

    assert loaded == original

    assert loaded.manager.value is not None
    assert loaded.manager.value.ico == "27437558"

    assert loaded.aum_history.value is not None
    assert len(loaded.aum_history.value.observations) == 2

    assert loaded.annual_returns.value is not None
    assert [item.year for item in loaded.annual_returns.value.observations] == [2024, 2025]

    assert loaded.historical_values.value is not None
    assert loaded.historical_values.value.series[0].share_class == "A"

    assert loaded.news.value is not None
    assert loaded.news.value.items[0].source_type is NewsSourceType.OFFICIAL_FUND


def test_output_written_before_version_three_still_loads(
    tmp_path: Path,
) -> None:
    """Backward compatibility: the new fields default to pending."""

    legacy = json.loads(OUTPUT_ADAPTER.dump_json([create_pending_fund(FUND)]))

    for field in (
        "manager",
        "administrator",
        "aum_history",
        "annual_returns",
        "historical_values",
        "news",
    ):
        legacy[0].pop(field)

    output_path = tmp_path / "legacy.json"

    output_path.write_text(
        json.dumps(
            legacy,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded = load_output(output_path)[0]

    assert loaded.manager.status is FieldStatus.PENDING
    assert loaded.aum_history.status is FieldStatus.PENDING
    assert loaded.news.status is FieldStatus.PENDING

    # The fields that existed before keep their meaning.
    assert loaded.investment_horizon.status is FieldStatus.PENDING
    assert loaded.name == FUND.name


def test_target_return_records_its_concept_and_period() -> None:
    from fundscraper.output_models import TargetReturnValue

    value = TargetReturnValue(
        minimum_percent_pa=8,
        return_type=ReturnType.HURDLE,
        annualization=Annualization.PER_ANNUM,
        period="po dobu investicni periody",
        share_class="PIA",
    )

    assert value.return_type is ReturnType.HURDLE
    assert value.share_class == "PIA"


def test_fee_collection_still_requires_one_item() -> None:
    with pytest.raises(ValidationError):
        FeeCollection(items=[])
