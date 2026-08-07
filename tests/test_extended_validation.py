from __future__ import annotations

from datetime import date

from fundscraper.extended_validation import (
    ValidationCode,
    ValidationSeverity,
    rejects,
    validate_annual_returns,
    validate_capital_observations,
    validate_fee_items,
    validate_historical_series,
    validate_minimum_investment,
    validate_news_items,
)
from fundscraper.output_models import (
    AnnualReturnObservation,
    AumMetricType,
    CapitalObservation,
    Confidence,
    FeeItem,
    FeeType,
    FundNewsItem,
    HistoricalValueObservation,
    HistoricalValueSeries,
    HistoricalValueType,
    InferenceReference,
    InferenceType,
    MinimumInvestmentKind,
    MinimumInvestmentValue,
    NewsSourceType,
    ReturnSeriesType,
    ValueOrigin,
)

FUND_NAME = "Rezidento Alfa SICAV, a.s."

FUND_WEB = "https://www.rezidentoalfa.cz"


def _codes(findings: object) -> set[str]:
    return {finding.code.value for finding in findings}  # type: ignore[attr-defined]


def test_refuses_a_statutory_minimum_reported_as_fund_assets() -> None:
    findings = validate_capital_observations(
        [
            CapitalObservation(
                amount=1_250_000,
                currency="EUR",
                metric_type=AumMetricType.STATUTORY_MINIMUM_CAPITAL,
                as_of=date(2024, 12, 31),
            )
        ]
    )

    assert ValidationCode.STATUTORY_CAPITAL_AS_AUM.value in _codes(findings)

    assert rejects(findings)


def test_refuses_manager_level_assets_reported_as_fund_assets() -> None:
    findings = validate_capital_observations(
        [
            CapitalObservation(
                amount=7_000_000_000,
                currency="CZK",
                metric_type=AumMetricType.MANAGER_AUM,
                as_of=date(2024, 12, 31),
            )
        ]
    )

    assert ValidationCode.MANAGER_AUM_AS_FUND_AUM.value in _codes(findings)


def test_refuses_a_capital_series_repeating_one_date() -> None:
    observation = CapitalObservation(
        amount=1_000_000,
        currency="CZK",
        metric_type=AumMetricType.NET_ASSETS,
        as_of=date(2024, 12, 31),
    )

    findings = validate_capital_observations(
        [
            observation,
            observation,
        ]
    )

    assert ValidationCode.DUPLICATE_DATES_IN_SERIES.value in _codes(findings)


def test_refuses_a_cumulative_return_among_the_annual_ones() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2024,
                return_percent=6.5,
            ),
            AnnualReturnObservation(
                year=2024,
                return_percent=26.6,
                series_type=ReturnSeriesType.CUMULATIVE,
            ),
        ]
    )

    assert ValidationCode.CUMULATIVE_AS_ANNUAL_RETURN.value in _codes(findings)


def test_refuses_a_kid_scenario_among_the_annual_returns() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2029,
                return_percent=4.8,
                series_type=ReturnSeriesType.KID_SCENARIO,
            )
        ]
    )

    assert ValidationCode.KID_SCENARIO_AS_ANNUAL_RETURN.value in _codes(findings)


def test_refuses_two_different_results_for_one_year_and_class() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2024,
                return_percent=6.5,
                share_class="PIA",
            ),
            AnnualReturnObservation(
                year=2024,
                return_percent=8.1,
                share_class="PIA",
            ),
        ]
    )

    assert ValidationCode.CONFLICTING_ANNUAL_RETURNS.value in _codes(findings)

    assert rejects(findings)


def test_accepts_the_same_year_for_two_different_classes() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2024,
                return_percent=6.5,
                share_class="PIA",
            ),
            AnnualReturnObservation(
                year=2024,
                return_percent=8.1,
                share_class="VIA",
            ),
        ]
    )

    assert not findings


def test_reports_one_quantity_split_across_two_series() -> None:
    def series(share_class: str | None) -> HistoricalValueSeries:
        return HistoricalValueSeries(
            value_type=HistoricalValueType.NAV_PER_SHARE,
            currency="CZK",
            share_class=share_class,
            observations=[
                HistoricalValueObservation(
                    as_of=date(2024, 12, 31),
                    value=1.5,
                    currency="CZK",
                )
            ],
        )

    findings = validate_historical_series(
        [
            series("PIA"),
            series(None),
        ]
    )

    assert ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES.value in _codes(findings)


def test_refuses_a_news_item_that_does_not_name_this_fund() -> None:
    findings = validate_news_items(
        items=[
            FundNewsItem(
                title="Výsledky fondu Beta za rok 2024",
                url="https://www.spravce.cz/aktuality/beta-2024",  # type: ignore[arg-type]
                source_domain="spravce.cz",
                source_type=NewsSourceType.MANAGER,
                relation_confidence=Confidence.LOW,
            )
        ],
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
    )

    assert ValidationCode.NEWS_OF_ANOTHER_FUND.value in _codes(findings)

    assert rejects(findings)


def test_accepts_a_news_item_of_the_official_fund_website() -> None:
    findings = validate_news_items(
        items=[
            FundNewsItem(
                title="Nový projekt v Brně",
                url="https://www.rezidentoalfa.cz/aktuality/novy-projekt",  # type: ignore[arg-type]
                source_domain="rezidentoalfa.cz",
                source_type=NewsSourceType.OFFICIAL_FUND,
                relation_confidence=Confidence.MEDIUM,
            )
        ],
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
    )

    assert not findings


def test_refuses_an_inferred_minimum_investment_without_a_legal_basis() -> None:
    # The output model already forbids this shape, so the value is built
    # unvalidated on purpose: the rule exists for a delivered file that
    # was written before the model did, which the audit reads as raw JSON.
    value = MinimumInvestmentValue.model_construct(
        amount=1_000_000,
        currency="CZK",
        kind=MinimumInvestmentKind.LEGAL_THRESHOLD,
        origin=ValueOrigin.INFERRED,
        inference=None,
    )

    findings = validate_minimum_investment(value)

    assert ValidationCode.INFERRED_MINIMUM_WITHOUT_BASIS.value in _codes(findings)

    assert rejects(findings)


def test_accepts_an_inferred_minimum_investment_that_states_its_basis() -> None:
    value = MinimumInvestmentValue(
        amount=1_000_000,
        currency="CZK",
        kind=MinimumInvestmentKind.LEGAL_THRESHOLD,
        origin=ValueOrigin.INFERRED,
        inference=InferenceReference(
            inference_type=InferenceType.LEGAL_DEFAULT,
            legal_basis="§ 272 odst. 1 ZISIF",
            jurisdiction="CZ",
            effective_date=date(2013, 8, 19),
        ),
    )

    assert not validate_minimum_investment(value)


def test_accepts_an_explicit_minimum_investment() -> None:
    value = MinimumInvestmentValue(
        amount=1_000_000,
        currency="CZK",
        kind=MinimumInvestmentKind.INITIAL_SUBSCRIPTION,
    )

    assert not validate_minimum_investment(value)


def test_reports_a_fee_range_collapsed_to_one_number() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.ENTRY,
                rate_percent=6.0,
                basis="Vstupní poplatek od 0 % do 6 % z výše investice",
            )
        ]
    )

    assert ValidationCode.COLLAPSED_FEE_RANGE.value in _codes(findings)

    assert all(finding.severity is ValidationSeverity.REVIEW for finding in findings)


def test_accepts_a_fee_that_keeps_both_bounds() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.ENTRY,
                rate_percent=6.0,
                maximum=True,
                minimum_rate_percent=0.0,
                maximum_rate_percent=6.0,
                basis="Vstupní poplatek od 0 % do 6 % z výše investice",
            )
        ]
    )

    assert not findings
