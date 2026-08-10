"""
Regression tests of the consolidated validation layer.

Every case here is a defect that was really delivered by this project, or
the value next to it that must keep passing. The occupancy percentage,
the model profit read as a guaranteed return, the internal rate of return
of a key information document, the value per investment share reported as
the assets of a fund and the four-trillion-crown fund capital were all
read from the 341-fund output before the rule that catches them existed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fundscraper.extended_validation import (
    FIELD_SPECIFICATIONS,
    FundRecordView,
    ValidationCode,
    ValidationSeverity,
    ValueProvenance,
    fallback_extraction_metadata,
    rejects,
    validate_annual_returns,
    validate_assets_under_management,
    validate_capital_observations,
    validate_cross_fields,
    validate_fallback_extraction,
    validate_fee_items,
    validate_historical_series,
    validate_investment_horizon,
    validate_minimum_investment,
    validate_provenance,
    validate_target_return,
)
from fundscraper.output_audit import (
    AuditStatus,
    audit_field,
)
from fundscraper.output_models import (
    AnnualReturnObservation,
    AssetsUnderManagementValue,
    AumMetricType,
    CapitalObservation,
    Confidence,
    FeeItem,
    FeeType,
    HistoricalValueObservation,
    HistoricalValueSeries,
    HistoricalValueType,
    InvestmentHorizonValue,
    MinimumInvestmentKind,
    MinimumInvestmentValue,
    ReturnSeriesType,
    ReturnType,
    TargetReturnValue,
    ValueOrigin,
)

TODAY = date(2026, 8, 10)

FUND_WEB = "https://www.examplefond.cz/"

FUND_NAME = "EXAMPLE fond SICAV, a.s."


def codes(findings: Any) -> set[str]:
    return {finding.code.value for finding in findings}


def severities(findings: Any) -> set[ValidationSeverity]:
    return {finding.severity for finding in findings}


# ---------------------------------------------------------------------------
# Values that must keep passing
# ---------------------------------------------------------------------------


def test_accepts_the_ordinary_values_of_a_czech_qualified_investor_fund() -> None:
    assert not validate_investment_horizon(InvestmentHorizonValue(recommended_years=5.0))

    assert not validate_minimum_investment(
        MinimumInvestmentValue(
            amount=1_000_000.0,
            currency="CZK",
            kind=MinimumInvestmentKind.INITIAL_SUBSCRIPTION,
        ),
        quote="Minimální investice 1 000 000 Kč",
    )

    assert not validate_target_return(
        TargetReturnValue(
            value_percent_pa=7.0,
            return_type=ReturnType.TARGET,
        ),
        quote="Cílový výnos fondu činí 7 % p.a.",
        source_url="https://www.examplefond.cz/statut.pdf",
    )

    assert not validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=530_168_000.0,
            currency="CZK",
            metric_type=AumMetricType.FUND_CAPITAL,
            as_of=date(2025, 12, 31),
        ),
        today=TODAY,
    )


def test_accepts_the_boundary_values_of_each_range() -> None:
    """The bounds themselves are legal; only what lies past them is not."""

    assert not validate_investment_horizon(InvestmentHorizonValue(recommended_years=30.0))

    assert codes(validate_investment_horizon(InvestmentHorizonValue(recommended_years=30.5))) == {
        ValidationCode.IMPLAUSIBLE_HORIZON.value
    }

    at_the_floor = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=1_000_000.0,
            currency="CZK",
            metric_type=AumMetricType.NET_ASSETS,
            as_of=date(2025, 12, 31),
        ),
        today=TODAY,
    )

    assert not at_the_floor

    below_the_floor = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=999_999.0,
            currency="CZK",
            metric_type=AumMetricType.NET_ASSETS,
            as_of=date(2025, 12, 31),
        ),
        today=TODAY,
    )

    assert ValidationCode.IMPLAUSIBLY_SMALL_AUM.value in codes(below_the_floor)


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------


def test_reports_a_value_without_a_source() -> None:
    findings = validate_provenance(
        ValueProvenance(
            field="minimum_investment",
            quote="Minimální investice 1 000 000 Kč",
        )
    )

    assert codes(findings) == {ValidationCode.MISSING_SOURCE.value}

    # The value stays: an untraceable value may still be the right one.
    assert not rejects(findings)


def test_reports_a_value_without_evidence_or_a_page() -> None:
    findings = validate_provenance(
        ValueProvenance(
            field="minimum_investment",
            source_url="https://www.examplefond.cz/statut.pdf",
            retrieved_at="2026-01-15T10:00:00Z",
        )
    )

    assert ValidationCode.MISSING_EVIDENCE.value in codes(findings)
    assert ValidationCode.MISSING_PAGE_REFERENCE.value in codes(findings)


def test_does_not_ask_a_web_page_for_a_page_number() -> None:
    findings = validate_provenance(
        ValueProvenance(
            field="minimum_investment",
            source_url="https://www.examplefond.cz/pro-investory",
            retrieved_at="2026-01-15T10:00:00Z",
            quote="Minimální investice 1 000 000 Kč",
            supporting_numbers=(1_000_000.0,),
        )
    )

    assert not findings


def test_reports_a_value_its_evidence_does_not_contain() -> None:
    findings = validate_provenance(
        ValueProvenance(
            field="target_return",
            source_url="https://www.examplefond.cz/aktuality/rust",
            retrieved_at="2026-01-15T10:00:00Z",
            quote="Fond investorům přináší avizovaný výnos.",
            supporting_numbers=(15.0,),
        )
    )

    assert codes(findings) == {ValidationCode.EVIDENCE_DOES_NOT_SUPPORT_VALUE.value}


def test_accepts_an_amount_written_in_the_unit_of_its_statement() -> None:
    """ "179 564 tis. Kc" is the evidence of 179 564 000, not a mismatch."""

    assert not validate_provenance(
        ValueProvenance(
            field="assets_under_management",
            source_url="https://www.examplefond.cz/vyrocni-zprava.pdf",
            retrieved_at="2026-01-15T10:00:00Z",
            quote="Výše fondového kapitálu: 179 564 tis. Kč",
            page=12,
            supporting_numbers=(179_564_000.0,),
        )
    )


def test_reports_a_scope_the_field_does_not_accept() -> None:
    findings = validate_provenance(
        ValueProvenance(
            field="assets_under_management",
            source_url="https://www.examplefond.cz/statut.pdf",
            retrieved_at="2026-01-15T10:00:00Z",
            quote="Fondový kapitál 530 168 000 Kč",
            page=4,
            scope_type="manager",
            supporting_numbers=(530_168_000.0,),
        )
    )

    assert codes(findings) == {ValidationCode.SCOPE_MISMATCH.value}


# ---------------------------------------------------------------------------
# Investment horizon
# ---------------------------------------------------------------------------


def test_refuses_a_calendar_year_read_as_a_holding_period() -> None:
    findings = validate_investment_horizon(InvestmentHorizonValue(recommended_years=2026.0))

    assert codes(findings) == {ValidationCode.CALENDAR_YEAR_AS_HORIZON.value}
    assert rejects(findings)


# ---------------------------------------------------------------------------
# Minimum investment
# ---------------------------------------------------------------------------


def test_reports_a_unit_price_read_as_a_minimum_investment() -> None:
    findings = validate_minimum_investment(
        MinimumInvestmentValue(
            amount=1.0,
            currency="EUR",
            kind=MinimumInvestmentKind.INITIAL_SUBSCRIPTION,
        )
    )

    assert ValidationCode.IMPLAUSIBLY_SMALL_MINIMUM_INVESTMENT.value in codes(findings)
    assert not rejects(findings)


def test_refuses_the_statutory_fund_capital_read_as_a_minimum_investment() -> None:
    findings = validate_minimum_investment(
        MinimumInvestmentValue(
            amount=1_250_000.0,
            currency="EUR",
            kind=MinimumInvestmentKind.INITIAL_SUBSCRIPTION,
        ),
        quote=(
            "Nejnižší přípustná výše fondového kapitálu činí 1 250 000 EUR "
            "podle § 30 odst. 1 ZISIF."
        ),
    )

    assert ValidationCode.STATUTORY_CAPITAL_AS_MINIMUM_INVESTMENT.value in codes(findings)
    assert rejects(findings)


def test_reports_an_inferred_minimum_without_a_legal_basis() -> None:
    # The output model already forbids the shape, so the value is built
    # without validation: the rule exists for raw delivered JSON, which
    # no model has passed through.
    value = MinimumInvestmentValue.model_construct(
        amount=1_000_000.0,
        currency="CZK",
        kind=MinimumInvestmentKind.LEGAL_THRESHOLD,
        origin=ValueOrigin.INFERRED,
        inference=None,
    )

    findings = validate_minimum_investment(value)

    assert ValidationCode.INFERRED_MINIMUM_WITHOUT_BASIS.value in codes(findings)
    assert rejects(findings)


# ---------------------------------------------------------------------------
# Target return
# ---------------------------------------------------------------------------


def test_refuses_an_occupancy_percentage_read_as_a_target_return() -> None:
    findings = validate_target_return(
        TargetReturnValue(
            value_percent_pa=98.0,
            return_type=ReturnType.TARGET,
        ),
        quote="Obsazenost portfolia dosahuje 98 %.",
        source_url="https://www.examplefond.cz/o-fondu",
    )

    assert ValidationCode.UNRELATED_PERCENTAGE_AS_TARGET_RETURN.value in codes(findings)
    assert rejects(findings)


def test_refuses_a_model_profit_read_as_a_guaranteed_minimum_return() -> None:
    """The real BRIXX defect: 41.84 % taken from a model-profit table."""

    findings = validate_target_return(
        TargetReturnValue(
            value_percent_pa=41.84,
            return_type=ReturnType.GUARANTEED_MINIMUM,
        ),
        quote=(
            "4 roky 1 milion Kč, přičemž pro účely výpočtu modelového zisku "
            "byl použit očekávaný minimální výnos PIA v příslušném období. 41,84 %"
        ),
        source_url="https://www.brixx.cz/admin/fileGet.aspx?f=blnldgkya&m=2",
    )

    assert ValidationCode.UNRELATED_PERCENTAGE_AS_TARGET_RETURN.value in codes(findings)


def test_refuses_a_kid_internal_rate_of_return_read_as_a_target() -> None:
    """The real NWD defect: a cost disclosure of a key information document."""

    findings = validate_target_return(
        TargetReturnValue(
            value_percent_pa=21.59,
            return_type=ReturnType.TARGET,
        ),
        quote=(
            "Dopad na vnitřní výnosnost (IRR) ... bude vaše předpokládaná "
            "míra vnitřní výnosnosti (IRR) činit 21,59 % před"
        ),
        source_url="https://nwd.cz/data/files/464-sdeleni-klicovych-informaci.pdf",
    )

    assert ValidationCode.KID_SCENARIO_AS_TARGET_RETURN.value in codes(findings)
    assert rejects(findings)


def test_refuses_a_calendar_year_read_as_a_target_return() -> None:
    findings = validate_target_return(
        TargetReturnValue(value_percent_pa=2026.0),
        quote="Statut účinný od roku 2026",
    )

    assert ValidationCode.YEAR_CAPTURED_AS_PERCENTAGE.value in codes(findings)
    assert rejects(findings)


def test_keeps_an_unusually_high_but_stated_target_for_review() -> None:
    findings = validate_target_return(
        TargetReturnValue(
            value_percent_pa=30.0,
            return_type=ReturnType.TARGET,
        ),
        quote="Cílový výnos fondu činí 30 % p.a.",
        source_url="https://www.examplefond.cz/statut.pdf",
    )

    assert ValidationCode.UNUSUALLY_HIGH_TARGET_RETURN.value in codes(findings)
    assert not rejects(findings)


def test_preserves_the_kinds_of_return_a_fund_can_state() -> None:
    """Each concept is a value of its own and none of them is refused."""

    for return_type in (
        ReturnType.EXPECTED,
        ReturnType.TARGET,
        ReturnType.PREFERRED,
        ReturnType.HURDLE,
        ReturnType.GUARANTEED_MINIMUM,
    ):
        assert not validate_target_return(
            TargetReturnValue(
                value_percent_pa=8.0,
                return_type=return_type,
            ),
            quote="Fond cílí na 8 % p.a.",
        )

    assert not validate_target_return(
        TargetReturnValue(
            minimum_percent_pa=7.0,
            maximum_percent_pa=10.0,
            return_type=ReturnType.RANGE,
        ),
        quote="Očekávaný výnos 7 % až 10 % p.a.",
    )


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


def test_reports_a_fee_range_collapsed_onto_one_number() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.ENTRY,
                rate_percent=0.0,
                basis="Vstupní poplatek od 0 % do 3 % z výše investice",
            )
        ]
    )

    assert ValidationCode.COLLAPSED_FEE_RANGE.value in codes(findings)
    assert severities(findings) == {ValidationSeverity.REVIEW}


def test_reports_an_amount_stored_as_a_fee_rate() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.ENTRY,
                rate_percent=500.0,
                basis="Vstupní poplatek činí 500 Kč za pokyn",
            )
        ]
    )

    assert ValidationCode.FEE_AMOUNT_RATE_MISMATCH.value in codes(findings)


def test_reports_a_rate_stored_as_a_fixed_fee_amount() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.MANAGEMENT,
                fixed_amount=2.0,
                currency="CZK",
                basis="Poplatek za obhospodařování činí 2 % z fondového kapitálu",
            )
        ]
    )

    assert ValidationCode.FEE_AMOUNT_RATE_MISMATCH.value in codes(findings)


def test_keeps_the_genuinely_high_fees_of_qualified_investor_funds() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.EXIT,
                rate_percent=30.0,
                basis="Výstupní poplatek 30 % při odkupu v prvním roce",
            ),
            FeeItem(
                type=FeeType.PERFORMANCE,
                rate_percent=45.0,
                basis="Výkonnostní odměna 45 % nad hurdle rate",
            ),
        ]
    )

    assert not findings


def test_reports_a_fee_rate_above_one_hundred_per_cent() -> None:
    findings = validate_fee_items(
        [
            FeeItem(
                type=FeeType.ENTRY,
                rate_percent=1_000.0,
                basis="1000",
            )
        ]
    )

    assert ValidationCode.IMPLAUSIBLE_FEE_RATE.value in codes(findings)


# ---------------------------------------------------------------------------
# Assets under management
# ---------------------------------------------------------------------------


def observation(
    amount: float,
    *,
    metric: AumMetricType = AumMetricType.NET_ASSETS,
    as_of: date = date(2025, 12, 31),
    currency: str = "CZK",
) -> CapitalObservation:
    return CapitalObservation(
        amount=amount,
        currency=currency,
        metric_type=metric,
        as_of=as_of,
    )


def test_reports_a_value_per_share_stored_as_the_assets_of_a_fund() -> None:
    findings = validate_capital_observations(
        [
            observation(
                0.9543,
                metric=AumMetricType.NAV,
            ),
            observation(
                0.9916,
                metric=AumMetricType.NAV,
                as_of=date(2025, 11, 30),
            ),
        ]
    )

    assert ValidationCode.PER_SHARE_VALUE_AS_AUM.value in codes(findings)
    assert not rejects(findings)


def test_refuses_manager_level_assets_reported_as_the_assets_of_the_fund() -> None:
    findings = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=42_000_000_000.0,
            currency="CZK",
            metric_type=AumMetricType.MANAGER_AUM,
            as_of=date(2025, 12, 31),
        ),
        today=TODAY,
    )

    assert ValidationCode.MANAGER_AUM_AS_FUND_AUM.value in codes(findings)
    assert rejects(findings)


def test_refuses_the_statutory_minimum_capital_reported_as_assets() -> None:
    findings = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=1_250_000.0,
            currency="EUR",
            metric_type=AumMetricType.STATUTORY_MINIMUM_CAPITAL,
            as_of=date(2025, 12, 31),
        ),
        today=TODAY,
    )

    assert ValidationCode.STATUTORY_CAPITAL_AS_AUM.value in codes(findings)
    assert rejects(findings)


def test_reports_a_unit_multiplier_applied_twice() -> None:
    """The real ARETE defect: 4.4 trillion crowns in one fund."""

    findings = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=4_412_052_506_840.0,
            currency="CZK",
            metric_type=AumMetricType.FUND_CAPITAL,
            as_of=date(2024, 9, 30),
        ),
        today=TODAY,
    )

    assert ValidationCode.IMPLAUSIBLY_LARGE_AUM.value in codes(findings)

    # The number is kept: it is a scale problem, not a wrong meaning.
    assert not rejects(findings)


def test_reports_a_stale_observation_without_deleting_it() -> None:
    findings = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=93_119_000.0,
            currency="CZK",
            metric_type=AumMetricType.EQUITY,
            as_of=date(2018, 12, 31),
        ),
        today=TODAY,
    )

    assert ValidationCode.STALE_AS_OF_DATE.value in codes(findings)
    assert not rejects(findings)


def test_reports_a_currency_outside_the_delivered_set() -> None:
    findings = validate_assets_under_management(
        AssetsUnderManagementValue(
            amount=530_000_000.0,
            currency="GBP",
            metric_type=AumMetricType.NET_ASSETS,
            as_of=date(2025, 12, 31),
        ),
        today=TODAY,
    )

    assert ValidationCode.UNSUPPORTED_CURRENCY.value in codes(findings)


# ---------------------------------------------------------------------------
# Annual returns
# ---------------------------------------------------------------------------


def test_refuses_two_different_results_for_one_year_and_class() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2024,
                return_percent=6.2,
                share_class="A",
            ),
            AnnualReturnObservation(
                year=2024,
                return_percent=9.8,
                share_class="A",
            ),
        ]
    )

    assert ValidationCode.CONFLICTING_ANNUAL_RETURNS.value in codes(findings)
    assert severities(findings) == {ValidationSeverity.CONFLICT}
    assert rejects(findings)


def test_refuses_a_cumulative_result_among_the_calendar_years() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2024,
                return_percent=6.2,
            ),
            AnnualReturnObservation(
                year=2024,
                return_percent=38.0,
                series_type=ReturnSeriesType.CUMULATIVE,
            ),
        ]
    )

    assert ValidationCode.CUMULATIVE_AS_ANNUAL_RETURN.value in codes(findings)
    assert rejects(findings)


def test_refuses_a_kid_scenario_among_the_calendar_years() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2024,
                return_percent=6.2,
            ),
            AnnualReturnObservation(
                year=2029,
                return_percent=22.0,
                series_type=ReturnSeriesType.KID_SCENARIO,
            ),
        ]
    )

    assert ValidationCode.KID_SCENARIO_AS_ANNUAL_RETURN.value in codes(findings)
    assert rejects(findings)


def test_reports_annual_results_that_mix_named_and_unnamed_share_classes() -> None:
    findings = validate_annual_returns(
        [
            AnnualReturnObservation(
                year=2023,
                return_percent=6.2,
                share_class="A",
            ),
            AnnualReturnObservation(
                year=2024,
                return_percent=7.1,
            ),
        ]
    )

    assert ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES.value in codes(findings)
    assert not rejects(findings)


# ---------------------------------------------------------------------------
# Historical values
# ---------------------------------------------------------------------------


def series(
    value_type: HistoricalValueType,
    values: list[tuple[date, float]],
    *,
    currency: str = "CZK",
    share_class: str | None = None,
) -> HistoricalValueSeries:
    return HistoricalValueSeries(
        value_type=value_type,
        currency=currency,
        share_class=share_class,
        observations=[
            HistoricalValueObservation(
                as_of=as_of,
                value=value,
                currency=currency,
            )
            for as_of, value in values
        ],
    )


def test_reports_one_quantity_split_across_two_series() -> None:
    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [(date(2024, 12, 31), 530_000_000.0)],
            ),
            series(
                HistoricalValueType.FUND_CAPITAL,
                [(date(2023, 12, 31), 480_000_000.0)],
            ),
        ]
    )

    assert ValidationCode.MIXED_METRICS_IN_SERIES.value in codes(findings)


def test_reports_value_series_that_mix_named_and_unnamed_share_classes() -> None:
    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.NAV_PER_SHARE,
                [(date(2024, 12, 31), 1.21)],
                share_class="A",
            ),
            series(
                HistoricalValueType.NAV_PER_SHARE,
                [(date(2024, 12, 31), 1.05)],
            ),
        ]
    )

    assert ValidationCode.MIXED_SHARE_CLASSES_IN_SERIES.value in codes(findings)


def test_reports_one_quantity_reported_in_two_currencies() -> None:
    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [(date(2024, 12, 31), 530_000_000.0)],
                share_class="A",
            ),
            series(
                HistoricalValueType.FUND_CAPITAL,
                [(date(2024, 12, 31), 21_000_000.0)],
                currency="EUR",
                share_class="B",
            ),
        ]
    )

    assert ValidationCode.MIXED_CURRENCIES_IN_SERIES.value in codes(findings)


def test_reports_per_share_values_inside_a_fund_level_series() -> None:
    """The real DIRECT VIGO defect: net assets of 0.09 CZK."""

    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_NET_ASSETS,
                [
                    (date(2024, 12, 31), 0.09),
                    (date(2023, 12, 31), 0.1),
                ],
            )
        ]
    )

    assert ValidationCode.PER_SHARE_VALUE_AS_AUM.value in codes(findings)


def test_refuses_a_capital_series_that_repeats_a_date() -> None:
    findings = validate_capital_observations(
        [
            observation(530_000_000.0),
            observation(541_000_000.0),
        ]
    )

    assert ValidationCode.DUPLICATE_DATES_IN_SERIES.value in codes(findings)
    assert rejects(findings)


# ---------------------------------------------------------------------------
# Cross-field rules
# ---------------------------------------------------------------------------


def test_refuses_assets_that_equal_the_value_of_one_investment_share() -> None:
    findings = validate_cross_fields(
        FundRecordView(
            fund_name=FUND_NAME,
            assets_under_management=AssetsUnderManagementValue(
                amount=1.0739,
                currency="CZK",
                metric_type=AumMetricType.FUND_AUM,
                as_of=date(2025, 12, 31),
            ),
            historical_series=(
                series(
                    HistoricalValueType.NAV_PER_SHARE,
                    [(date(2025, 12, 31), 1.0739)],
                ),
            ),
        )
    )

    assert ValidationCode.PER_SHARE_VALUE_AS_AUM.value in codes(findings)
    assert rejects(findings)


def test_reports_delivered_assets_that_contradict_the_capital_series() -> None:
    findings = validate_cross_fields(
        FundRecordView(
            fund_name=FUND_NAME,
            assets_under_management=AssetsUnderManagementValue(
                amount=35_661_000.0,
                currency="CZK",
                metric_type=AumMetricType.EQUITY,
                as_of=date(2024, 12, 31),
            ),
            aum_observations=(
                observation(
                    3_163_192_000.0,
                    metric=AumMetricType.EQUITY,
                    as_of=date(2024, 12, 31),
                ),
            ),
        )
    )

    assert ValidationCode.CONFLICTING_VALUES.value in codes(findings)
    assert severities(findings) == {ValidationSeverity.CONFLICT}


def test_accepts_two_different_metrics_of_the_same_date() -> None:
    """Total assets and net assets differ by the liabilities of the fund."""

    assert not validate_cross_fields(
        FundRecordView(
            fund_name=FUND_NAME,
            assets_under_management=AssetsUnderManagementValue(
                amount=530_168_000.0,
                currency="CZK",
                metric_type=AumMetricType.NET_ASSETS,
                as_of=date(2024, 12, 31),
            ),
            aum_observations=(
                observation(
                    612_400_000.0,
                    metric=AumMetricType.ASSETS_TOTAL,
                    as_of=date(2024, 12, 31),
                ),
            ),
        )
    )


def test_reports_a_target_return_that_repeats_an_achieved_result() -> None:
    findings = validate_cross_fields(
        FundRecordView(
            fund_name=FUND_NAME,
            target_return=TargetReturnValue(
                value_percent_pa=6.2,
                return_type=ReturnType.TARGET,
            ),
            annual_returns=(
                AnnualReturnObservation(
                    year=2024,
                    return_percent=6.2,
                ),
            ),
        )
    )

    assert ValidationCode.HISTORICAL_RETURN_AS_TARGET_RETURN.value in codes(findings)
    assert not rejects(findings)


# ---------------------------------------------------------------------------
# The layout fallback
# ---------------------------------------------------------------------------


def test_a_fallback_value_is_marked_for_review_and_never_high() -> None:
    confidence, review_required = fallback_extraction_metadata(
        Confidence.HIGH,
        placed_on_a_page=True,
    )

    assert confidence is Confidence.MEDIUM
    assert review_required is True

    assert not validate_fallback_extraction(
        is_fallback=True,
        confidence=confidence,
        review_required=review_required,
        placed_on_a_page=True,
    )


def test_a_fallback_value_without_a_page_is_worth_less_still() -> None:
    confidence, review_required = fallback_extraction_metadata(
        Confidence.MEDIUM,
        placed_on_a_page=False,
    )

    assert confidence is Confidence.LOW
    assert review_required is True


def test_reports_a_fallback_value_delivered_with_high_confidence() -> None:
    findings = validate_fallback_extraction(
        is_fallback=True,
        confidence=Confidence.HIGH,
        review_required=False,
        placed_on_a_page=True,
    )

    assert codes(findings) == {ValidationCode.FALLBACK_PARSER_REVIEW_REQUIRED.value}
    assert len(findings) == 2


def test_leaves_a_primary_parser_value_alone() -> None:
    assert not validate_fallback_extraction(
        is_fallback=False,
        confidence=Confidence.HIGH,
        review_required=False,
        placed_on_a_page=True,
    )


# ---------------------------------------------------------------------------
# The audit reports what the rules find
# ---------------------------------------------------------------------------


def evidence_payload(
    value: Any,
    *,
    url: str = "https://www.examplefond.cz/statut.pdf",
    quote: str = "",
    page: int | None = 4,
    scope_type: str = "fund",
) -> dict[str, Any]:
    return {
        "status": "found",
        "value": value,
        "raw_value": quote,
        "scope": {"type": scope_type},
        "source": {
            "source": {
                "url": url,
                "document_type": "statute",
                "retrieved_at": "2026-01-15T10:00:00Z",
            },
            "quote": quote,
            "page": page,
        },
        "extraction": {
            "method": "regex",
            "confidence": "medium",
            "review_required": False,
        },
    }


def audit_one(
    field: str,
    payload: Any,
) -> Any:
    return audit_field(
        fund_id="fund_0000000000000000",
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        field=field,
        payload=payload,
        shared_sources={},
        today=TODAY,
    )


def test_the_audit_rejects_an_unrelated_percentage_and_keeps_the_value() -> None:
    outcome = audit_one(
        "target_return",
        evidence_payload(
            {
                "value_percent_pa": 98.0,
                "return_type": "target",
            },
            quote="Obsazenost portfolia dosahuje 98 %.",
        ),
    )

    assert outcome.status is AuditStatus.REJECTED

    finding = next(
        item
        for item in outcome.findings
        if item.reason_code == ValidationCode.UNRELATED_PERCENTAGE_AS_TARGET_RETURN.value
    )

    # Nothing is deleted: the value, its source and its evidence are all
    # reported next to the reason a reviewer should not trust them.
    assert finding.extracted_value["value_percent_pa"] == 98.0
    assert finding.source_url == "https://www.examplefond.cz/statut.pdf"
    assert finding.evidence == "Obsazenost portfolia dosahuje 98 %."
    assert finding.source_scope == "fund"
    assert finding.severity is ValidationSeverity.REJECT


def test_the_audit_accepts_a_well_supported_value() -> None:
    outcome = audit_one(
        "minimum_investment",
        evidence_payload(
            {
                "amount": 1_000_000.0,
                "currency": "CZK",
                "kind": "initial_subscription",
            },
            quote="Minimální investice do fondu činí 1 000 000 Kč.",
        ),
    )

    assert outcome.status is AuditStatus.VALID
    assert outcome.findings == ()


def test_every_audited_field_has_a_published_specification() -> None:
    from fundscraper.output_audit import ALL_AUDITED_FIELDS

    assert set(FIELD_SPECIFICATIONS) == set(ALL_AUDITED_FIELDS)

    for specification in FIELD_SPECIFICATIONS.values():
        assert specification.notes
        assert specification.accepted_scopes
