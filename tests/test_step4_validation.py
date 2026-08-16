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
    validate_capital_attribution,
    validate_capital_metric_wording,
    validate_capital_observations,
    validate_cross_fields,
    validate_fallback_extraction,
    validate_fee_items,
    validate_historical_series,
    validate_horizon_wording,
    validate_investment_horizon,
    validate_minimum_investment,
    validate_party,
    validate_provenance,
    validate_return_wording,
    validate_target_return,
)
from fundscraper.output_audit import (
    AuditStatus,
    audit_field,
)
from fundscraper.output_models import (
    Annualization,
    AnnualReturnObservation,
    AssetsUnderManagementValue,
    AumMetricType,
    CapitalObservation,
    Confidence,
    FeeItem,
    FeeType,
    FundParty,
    HistoricalValueObservation,
    HistoricalValueSeries,
    HistoricalValueType,
    HorizonKind,
    InvestmentHorizonValue,
    MinimumInvestmentKind,
    MinimumInvestmentValue,
    PartyRole,
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


# ---------------------------------------------------------------------------
# Guards that keep a doubted value out of the delivery
# ---------------------------------------------------------------------------
#
# The three below are the ones 3M FUND MSI SICAV a.s. was delivered
# against: a value series carrying 2026-12-31 and 2027-12-31, a fund
# capital of 0.5 CZK, and the thousands multiplier of an annual report
# left unapplied. Each has to reach an audit status the delivery
# withholds, because a rule nobody acts on delivers the same defect.


def test_refuses_a_value_series_that_reports_days_that_have_not_happened() -> None:
    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.INVESTMENT_SHARE_VALUE,
                [
                    (date(2026, 12, 31), 1.0739),
                    (date(2027, 12, 31), 1.1204),
                ],
                share_class="S",
            )
        ],
        today=TODAY,
    )

    assert ValidationCode.FUTURE_AS_OF_DATE.value in codes(findings)

    detail = next(
        finding.detail for finding in findings if finding.code is ValidationCode.FUTURE_AS_OF_DATE
    )

    assert "2026-12-31" in detail
    assert "2027-12-31" in detail


def test_refuses_a_capital_series_that_reports_days_that_have_not_happened() -> None:
    findings = validate_capital_observations(
        [
            observation(
                530_000_000.0,
                as_of=date(2025, 12, 31),
            ),
            observation(
                610_000_000.0,
                as_of=date(2027, 6, 30),
            ),
        ],
        today=TODAY,
    )

    assert ValidationCode.FUTURE_AS_OF_DATE.value in codes(findings)


def test_a_dated_series_wholly_in_the_past_stays_quiet() -> None:
    assert ValidationCode.FUTURE_AS_OF_DATE.value not in codes(
        validate_historical_series(
            [
                series(
                    HistoricalValueType.NAV_PER_SHARE,
                    [
                        (date(2024, 12, 31), 1.0739),
                        (date(2025, 12, 31), 1.1204),
                    ],
                    share_class="S",
                )
            ],
            today=TODAY,
        )
    )


def test_refuses_a_value_per_share_stored_as_the_capital_of_the_fund() -> None:
    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [
                    (date(2024, 12, 31), 0.5),
                    (date(2025, 12, 31), 0.53),
                ],
            )
        ],
        today=TODAY,
    )

    assert ValidationCode.PER_SHARE_VALUE_AS_AUM.value in codes(findings)


def test_the_future_dated_series_of_the_delivered_fund_is_not_valid() -> None:
    """The whole chain: the rule fires, the audit doubts, delivery withholds."""

    from fundscraper.delivery_export import BLOCKING_AUDIT_STATUSES

    outcome = audit_one(
        "historical_values",
        evidence_payload(
            {
                "series": [
                    {
                        "value_type": "investment_share_value",
                        "currency": "CZK",
                        "share_class": "S",
                        "observations": [
                            {
                                "as_of": "2026-12-31",
                                "value": 1.0739,
                                "currency": "CZK",
                            },
                            {
                                "as_of": "2027-12-31",
                                "value": 1.1204,
                                "currency": "CZK",
                            },
                        ],
                    }
                ]
            },
            quote="Hodnota investiční akcie třídy S",
        ),
    )

    assert outcome.status is not AuditStatus.VALID

    assert ValidationCode.FUTURE_AS_OF_DATE.value in {
        finding.reason_code for finding in outcome.findings
    }

    assert outcome.status.value in BLOCKING_AUDIT_STATUSES


def test_an_unapplied_thousands_multiplier_is_not_valid() -> None:
    from fundscraper.delivery_export import BLOCKING_AUDIT_STATUSES

    outcome = audit_one(
        "assets_under_management",
        evidence_payload(
            {
                "amount": 12_202.0,
                "currency": "CZK",
                "metric_type": "net_assets",
                "as_of": "2025-12-31",
            },
            url="https://www.examplefond.cz/vyrocni_zprava_2025.pdf",
            quote="Aktiva celkem 12 202",
        ),
    )

    assert outcome.status is not AuditStatus.VALID

    assert ValidationCode.THOUSANDS_UNIT_NOT_APPLIED.value in {
        finding.reason_code for finding in outcome.findings
    }

    assert outcome.status.value in BLOCKING_AUDIT_STATUSES


# ---------------------------------------------------------------------------
# The second correctness pass: classes that survived the first audit
# ---------------------------------------------------------------------------


def test_refuses_the_capital_of_a_company_the_fund_holds() -> None:
    """
    The 119.35 billion CZK 3M FUND MSI SICAV a.s. delivered as its own.

    The conversion of "119 350 mil. Kc" is exactly right. What the
    evidence never says is that the money is the fund's: the sentence
    reduces the equity of MS Trnita 1 s.r.o., a company the fund holds.
    """

    findings = validate_capital_attribution(
        amounts=[119_350_000_000.0],
        quote=(
            "Na základě souhlasu statutárního\norgánu byl dne 3.7.2023 "
            "snížen vlastní kapitál společnosti MS Trnitá 1 s.r.o. "
            "o 119 350 mil. Kč."
        ),
        fund_name="3M FUND MSI SICAV a.s.",
    )

    assert ValidationCode.CAPITAL_OF_ANOTHER_COMPANY.value in codes(findings)
    assert rejects(findings)


def test_keeps_capital_the_evidence_gives_to_the_fund_itself() -> None:
    """
    The auditor named in the same paragraph must not cost the fund its value.

    A Czech annual report names its auditor a line above the figures. The
    amount here belongs to "fond", which stands closer to it than the
    audit firm does.
    """

    assert not validate_capital_attribution(
        amounts=[980_412_000.0],
        quote=(
            "Hospodaření fondu bylo ověřeno auditorskou společností "
            "APOGEO AUDIT s.r.o.\nK 31. 12. 2020 fond vykázal celková "
            "aktiva ve výši 980 412 tis. Kč"
        ),
        fund_name="TOLAR SICAV a. s.",
    )


def test_stays_silent_when_the_amount_is_not_in_the_evidence() -> None:
    """An amount nobody can locate proves nothing about who owns it."""

    assert not validate_capital_attribution(
        amounts=[531_000_000.0],
        quote="Zajištění provádí J & T BANKA, a.s. pro tento fond.",
        fund_name="FIDUROCK Retail Parks Fund SICAV, a.s.",
    )


def test_refuses_a_party_name_that_opens_with_a_date() -> None:
    findings = validate_party(
        party=FundParty(
            role=PartyRole.ADMINISTRATOR,
            name="4. 10. 2021 AVANT investiční společnost, a.s.",
        ),
        expected_role=PartyRole.ADMINISTRATOR,
        fund_name=FUND_NAME,
    )

    assert ValidationCode.PARTY_NAME_CONTAINS_A_DATE.value in codes(findings)


def test_refuses_a_party_name_that_opens_with_a_bare_year() -> None:
    findings = validate_party(
        party=FundParty(
            role=PartyRole.ADMINISTRATOR,
            name="2025 investiční společnost",
        ),
        expected_role=PartyRole.ADMINISTRATOR,
        fund_name=FUND_NAME,
    )

    assert ValidationCode.PARTY_NAME_CONTAINS_A_DATE.value in codes(findings)


def test_keeps_a_company_whose_own_name_begins_with_a_digit() -> None:
    """A digit is not a date. Real Czech companies open with one."""

    findings = validate_party(
        party=FundParty(
            role=PartyRole.MANAGER,
            name="4stavební a.s.",
        ),
        expected_role=PartyRole.MANAGER,
        fund_name=FUND_NAME,
    )

    assert ValidationCode.PARTY_NAME_CONTAINS_A_DATE.value not in codes(findings)


def test_the_name_cleaner_separates_a_date_from_the_company() -> None:
    """The root cause: a leading digit was taken to open a legal name."""

    from fundscraper.field_definitions import _without_leading_date

    assert (
        _without_leading_date("4. 10. 2021 AVANT investiční společnost, a.s.")
        == "AVANT investiční společnost, a.s."
    )
    assert _without_leading_date("23.05.2025 AVANT investiční společnost, a.s.") == (
        "AVANT investiční společnost, a.s."
    )
    assert _without_leading_date("2025 investiční společnost") == "investiční společnost"

    # Digits that belong to the company survive untouched.
    assert _without_leading_date("3M FUND MSI SICAV a.s.") == "3M FUND MSI SICAV a.s."
    assert _without_leading_date("4stavební a.s.") == "4stavební a.s."
    assert _without_leading_date("2025") == "2025"


def test_a_fund_capital_series_in_thousands_is_not_valid() -> None:
    """
    ARETE ENERGY TRANSITION delivered a fund capital of 24 245 CZK.

    That is not a per-share price and not a fund's capital either. It is
    an annual report published "v tis. Kc" whose multiplier was lost, and
    the series had no magnitude rule at all until now.
    """

    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [(date(2024, 12, 31), 24_245.0)],
            )
        ],
        today=TODAY,
    )

    assert ValidationCode.IMPLAUSIBLY_SMALL_AUM.value in codes(findings)


def test_an_indexed_value_series_around_one_thousand_is_not_valid() -> None:
    """Max Realitni Fond: 1000, 1005, 1014, 1018 CZK of fund capital."""

    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [
                    (date(2023, 12, 31), 1_000.0),
                    (date(2024, 6, 30), 1_005.0),
                    (date(2024, 12, 31), 1_014.0),
                ],
            )
        ],
        today=TODAY,
    )

    assert ValidationCode.IMPLAUSIBLY_SMALL_AUM.value in codes(findings)


def test_a_fund_sized_capital_series_keeps_passing() -> None:
    """The boundary: the rule must not swallow a real fund."""

    assert not validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [
                    (date(2023, 12, 31), 1_000_000.0),
                    (date(2024, 12, 31), 530_000_000.0),
                ],
            )
        ],
        today=TODAY,
    )


def test_a_per_share_series_is_still_named_a_per_share_series() -> None:
    """Below the per-share bound the older, more precise reason wins."""

    findings = validate_historical_series(
        [
            series(
                HistoricalValueType.FUND_CAPITAL,
                [(date(2024, 12, 31), 0.5)],
            )
        ],
        today=TODAY,
    )

    assert ValidationCode.PER_SHARE_VALUE_AS_AUM.value in codes(findings)
    assert ValidationCode.IMPLAUSIBLY_SMALL_AUM.value not in codes(findings)


def test_refuses_a_rate_per_annum_no_source_ever_called_annual() -> None:
    findings = validate_target_return(
        TargetReturnValue(
            value_percent_pa=10.0,
            return_type=ReturnType.EXPECTED,
            annualization=Annualization.UNKNOWN,
        ),
        quote="Expected return 10%",
        source_url="https://www.examplefond.cz/",
    )

    assert ValidationCode.UNKNOWN_ANNUALIZATION_FOR_ANNUAL_RATE.value in codes(findings)


def test_refuses_an_unannualized_range_as_well_as_a_single_rate() -> None:
    findings = validate_target_return(
        TargetReturnValue(
            minimum_percent_pa=4.0,
            maximum_percent_pa=7.0,
            return_type=ReturnType.RANGE,
            annualization=Annualization.UNKNOWN,
        ),
        quote="očekávaný výnos 4 - 7 %",
        source_url="https://www.examplefond.cz/",
    )

    assert ValidationCode.UNKNOWN_ANNUALIZATION_FOR_ANNUAL_RATE.value in codes(findings)


def test_keeps_a_rate_the_source_states_per_annum() -> None:
    assert ValidationCode.UNKNOWN_ANNUALIZATION_FOR_ANNUAL_RATE.value not in codes(
        validate_target_return(
            TargetReturnValue(
                value_percent_pa=7.0,
                return_type=ReturnType.TARGET,
                annualization=Annualization.PER_ANNUM,
            ),
            quote="Cílový výnos fondu činí 7 % p.a.",
            source_url="https://www.examplefond.cz/statut.pdf",
        )
    )


def test_the_annualization_of_an_annual_target_is_read_from_the_wording() -> None:
    """
    "Target annual return 30%" was classified as unknown.

    The period stands in the name of the figure as often as it stands in
    a p.a. suffix.
    """

    from fundscraper.field_definitions import classify_annualization

    assert classify_annualization("target annual return 30%") is Annualization.PER_ANNUM
    assert classify_annualization("rocni vynos 6 %") is Annualization.PER_ANNUM
    assert classify_annualization("ocekavany vynos 10 % p.a.") is Annualization.PER_ANNUM

    # A bare percentage stays unknown, and an annual report is not a rate.
    assert classify_annualization("expected return 10%") is Annualization.UNKNOWN
    assert classify_annualization("vyrocni zprava 2024 vynos 8 %") is Annualization.UNKNOWN


# ---------------------------------------------------------------------------
# The third pass: what manual review of the delivered sample found
# ---------------------------------------------------------------------------


def test_a_minimum_horizon_must_not_be_delivered_as_an_exact_one() -> None:
    """
    CREDITAS OPPORTUNITY: "Investicni horizont: min. 3 roky".

    Delivered as an exact three years, which tells an investor to plan
    for what the fund calls the least it will accept.
    """

    findings = validate_horizon_wording(
        InvestmentHorizonValue(recommended_years=3.0),
        quote="fond kvalifikovaných investorů\nInvestiční horizont:\nmin. 3 roky",
    )

    assert ValidationCode.HORIZON_BOUND_LOST.value in codes(findings)


def test_the_same_horizon_stored_as_a_minimum_keeps_passing() -> None:
    assert not validate_horizon_wording(
        InvestmentHorizonValue(
            recommended_years=3.0,
            kind=HorizonKind.MINIMUM,
            minimum_years=3.0,
        ),
        quote="Investiční horizont: min. 3 roky",
    )


def test_an_exact_horizon_from_exact_wording_keeps_passing() -> None:
    """The neighbouring value: no bound stated, none expected."""

    assert not validate_horizon_wording(
        InvestmentHorizonValue(recommended_years=5.0),
        quote="Doporučený investiční horizont je 5 let.",
    )


def test_a_horizon_written_as_five_years_and_more_is_a_minimum() -> None:
    findings = validate_horizon_wording(
        InvestmentHorizonValue(recommended_years=5.0),
        quote="investiční horizont činí 5 let a více",
    )

    assert ValidationCode.HORIZON_BOUND_LOST.value in codes(findings)


def test_the_extractor_reads_the_bound_from_the_wording() -> None:
    """The root cause: every horizon was built as exact."""

    from fundscraper.field_definitions import classify_horizon_kind

    assert classify_horizon_kind("investicni horizont: min. 3 roky") is HorizonKind.MINIMUM
    assert classify_horizon_kind("investicni horizont cini 5 let a vice") is HorizonKind.MINIMUM
    assert classify_horizon_kind("recommended holding period at least 7 years") is (
        HorizonKind.MINIMUM
    )
    assert classify_horizon_kind("doporuceny investicni horizont 5 let") is HorizonKind.EXACT


def test_fund_capital_must_not_be_delivered_as_equity() -> None:
    """
    FOND CESKYCH KORPORATNICH DLUHOPISU: "Fondovy kapital ... 52 012 tis. Kc".

    The amount, the scaling and the date were all right. The metric was
    not, and the fund's own history held the same figure, on the same
    day, as fund capital.
    """

    findings = validate_capital_metric_wording(
        metric_type=AumMetricType.EQUITY,
        quote="Fondový kapitál Společnosti dosáhl k 31. 12. 2022 hodnoty 52 012 tis. Kč.",
    )

    assert ValidationCode.CAPITAL_METRIC_CONTRADICTS_EVIDENCE.value in codes(findings)


def test_the_metrics_a_source_names_are_kept_apart() -> None:
    """Each label keeps its own metric; equal amounts do not merge them."""

    from fundscraper.field_definitions import classify_capital_metric

    assert classify_capital_metric("fondovy kapital spolecnosti") is AumMetricType.FUND_CAPITAL
    assert classify_capital_metric("cista aktiva fondu") is AumMetricType.NET_ASSETS
    assert classify_capital_metric("cista hodnota aktiv") is AumMetricType.NAV
    assert classify_capital_metric("vlastni kapital fondu") is AumMetricType.EQUITY


def test_a_metric_that_agrees_with_its_evidence_keeps_passing() -> None:
    assert not validate_capital_metric_wording(
        metric_type=AumMetricType.FUND_CAPITAL,
        quote="Fondový kapitál Společnosti dosáhl hodnoty 52 012 tis. Kč.",
    )


def test_a_generic_assets_label_does_not_contradict_the_assets_metric() -> None:
    """Two names for the same thing are not a disagreement."""

    assert not validate_capital_metric_wording(
        metric_type=AumMetricType.FUND_AUM,
        quote="Aktiva ve správě fondu dosáhla 530 mil. Kč.",
    )


def test_a_benchmark_linked_return_must_not_be_delivered_as_a_fixed_rate() -> None:
    """
    AVANT Finance: "zhodnoceni 2TR + 1 % p.a." delivered as 1 % p.a.

    The spread alone is not a smaller version of the promise; it is a
    different one, and the stored model cannot hold the reference rate.
    """

    findings = validate_return_wording(
        TargetReturnValue(
            value_percent_pa=1.0,
            return_type=ReturnType.TARGET,
            share_class="PIA",
        ),
        quote=("přednostně do růstu PIA až do výše jejich zhodnocení 2TR + 1 % p.a."),
    )

    assert ValidationCode.BENCHMARK_RETURN_AS_FIXED_RATE.value in codes(findings)
    assert rejects(findings)


def test_a_rate_over_pribor_is_refused_the_same_way() -> None:
    findings = validate_return_wording(
        TargetReturnValue(
            value_percent_pa=3.0,
            return_type=ReturnType.TARGET,
        ),
        quote="Výnos je stanoven jako PRIBOR + 3 % p.a.",
    )

    assert ValidationCode.BENCHMARK_RETURN_AS_FIXED_RATE.value in codes(findings)


def test_a_genuinely_fixed_target_return_keeps_passing() -> None:
    assert not validate_return_wording(
        TargetReturnValue(
            value_percent_pa=7.0,
            return_type=ReturnType.TARGET,
        ),
        quote="Cílový výnos fondu činí 7 % p.a.",
    )


def test_the_extractor_refuses_to_reduce_a_formula_to_its_spread() -> None:
    """The root cause: the window yields nothing rather than the spread."""

    from fundscraper.field_definitions import states_benchmark_linked_return

    assert states_benchmark_linked_return("zhodnoceni 2tr + 1 % p.a.")
    assert states_benchmark_linked_return("vynos pribor + 3 %")
    assert states_benchmark_linked_return("euribor + 2,5 % rocne")
    assert states_benchmark_linked_return("inflace + 2 %")

    assert not states_benchmark_linked_return("cilovy vynos 7 % p.a.")


def test_refuses_the_net_assets_of_a_company_the_fund_acquired() -> None:
    """
    EBM Real Estate delivered 31 596 tis. Kc as its own assets.

    The figure is the net assets of an acquired company at the
    acquisition date, in a paragraph about Logport Development s.r.o.
    Two things had hidden it: the report groups thousands with a full
    stop rather than a space, and "EBM Partner a.s." counted as the fund
    itself because a three-letter brand prefix was treated as identity.
    """

    findings = validate_capital_attribution(
        amounts=[31_596_000.0],
        quote=(
            "Ve společnosti Logport Development, s.r.o. a eviduje EBM "
            "Partner a.s. finanční investici se 49% podílem na hlasovacích "
            "právech. Zbývající podíl ve výši 51 70.000 tis. Kč k datu "
            "akvizice. Čistá aktiva k datu akvizice byla ve výši 31.596 "
            "tis. Kč."
        ),
        fund_name="EBM Real Estate SICAV, a.s.",
    )

    assert ValidationCode.CAPITAL_OF_ANOTHER_COMPANY.value in codes(findings)


def test_an_amount_grouped_with_full_stops_is_still_located() -> None:
    """A Czech report groups thousands either way; the rule reads both."""

    from fundscraper.extended_validation import _written_amount_forms

    forms = _written_amount_forms(31_596_000.0)

    assert "31 596" in forms
    assert "31.596" in forms
    assert "31596" in forms


def test_a_negated_capital_label_does_not_supply_the_fund_assets() -> None:
    """
    "z toho neinvesticni fondovy kapital: 100 000 Kc" is a component.

    Five delivered funds carried that component as their assets — VALOUR
    100 000 CZK, VENDEAVOUR 29 950, SALUTEM 30 000, Safety Real 34 000
    and WF Group 66 720 000 — while their real capital ran to hundreds of
    millions. The phrase also contains the label "investicni fondovy
    kapital", so a substring search read the negation as the thing it
    negates.
    """

    from fundscraper.field_extraction import _money_after_label

    labels = ("fondovy kapital", "cista aktiva", "hodnota majetku")

    match = _money_after_label(
        normalized=(
            "z toho neinvesticni fondovy kapital: 100 000 kc "
            "(z toho 100 000 kc zapisovany zakladni kapital) "
            "z toho investicni fondovy kapital: 452 300 000 kc"
        ),
        labels=labels,
    )

    assert match is not None
    assert "452 300 000" in match.group(0)


def test_an_unqualified_capital_label_still_supplies_the_amount() -> None:
    """The boundary: only the negated occurrence is skipped."""

    from fundscraper.field_extraction import _money_after_label

    match = _money_after_label(
        normalized="fondovy kapital spolecnosti dosahl k 31.12.2022 hodnoty 52 012 tis. kc",
        labels=("fondovy kapital",),
    )

    assert match is not None
    assert "52 012" in match.group(0)


def test_only_the_named_qualifiers_negate_a_capital_label() -> None:
    from fundscraper.field_definitions import label_is_negated

    text = "z toho neinvesticni fondovy kapital: 100 000 kc"

    assert label_is_negated(
        normalized=text,
        label_start=text.index("fondovy kapital"),
    )

    plain = "celkovy fondovy kapital fondu"

    assert not label_is_negated(
        normalized=plain,
        label_start=plain.index("fondovy kapital"),
    )


# ---------------------------------------------------------------------------
# Dating a value that names its period instead of its day
# ---------------------------------------------------------------------------


def test_the_shared_vocabulary_carries_the_inflected_capital_forms() -> None:
    """
    "vyse fondoveho kapitalu" is how every AVANT statute states the total.

    The assets extractor used to carry its own shorter list that knew
    only the nominative "fondovy kapital", so the headline figure was
    invisible to it while the metric classifier read it correctly.
    """

    from fundscraper.field_definitions import FUND_CAPITAL_READING_LABELS

    assert "vyse fondoveho kapitalu" in FUND_CAPITAL_READING_LABELS
    assert "fondoveho kapitalu" in FUND_CAPITAL_READING_LABELS
    assert "fondovy kapital" in FUND_CAPITAL_READING_LABELS

    # A registered or statutory minimum capital is not this field, and
    # neither is a balance-sheet line every company has: "vlastni
    # kapital" in a manager's own annual report is the manager's equity.
    assert "zapisovany zakladni kapital" not in FUND_CAPITAL_READING_LABELS
    assert "minimalni kapital" not in FUND_CAPITAL_READING_LABELS
    assert "vlastni kapital" not in FUND_CAPITAL_READING_LABELS
    assert "aktiva celkem" not in FUND_CAPITAL_READING_LABELS


def test_the_negation_guard_survives_the_wider_vocabulary() -> None:
    """The real VALOUR window: the total, never the non-investment part."""

    from fundscraper.field_definitions import FUND_CAPITAL_READING_LABELS
    from fundscraper.field_extraction import _money_after_label

    match = _money_after_label(
        normalized=(
            "a) zakladni kapital fondu vyse fondoveho kapitalu: 672 417 510 kc "
            "(k poslednimu dni ucetniho obdobi) "
            "z toho neinvesticni fondovy kapital: 100 000 kc "
            "(z toho 100 000 kc zapisovany zakladni kapital) "
            "z toho investicni fondovy kapital: 672 317 510 kc"
        ),
        labels=FUND_CAPITAL_READING_LABELS,
    )

    assert match is not None
    assert "672 417 510" in match.group(0)
    assert "100 000" not in match.group(0)


def test_period_end_wording_is_recognised_and_plain_dates_are_not() -> None:
    from fundscraper.field_definitions import refers_to_period_end

    assert refers_to_period_end("vyse fondoveho kapitalu (k poslednimu dni ucetniho obdobi)")
    assert refers_to_period_end("cista aktiva k rozvahovemu dni")
    assert refers_to_period_end("net assets as at the balance sheet date")

    # A window that writes its date needs no substitution.
    assert not refers_to_period_end("fondovy kapital k 31.12.2022 cinil 52 012 tis. kc")

    # Nothing about publication makes a value dated.
    assert not refers_to_period_end("vyrocni zprava zverejnena dne 30.4.2025")


def test_a_published_date_is_never_taken_as_the_value_date() -> None:
    """
    The substitution the fallback must refuse.

    When a report was published says nothing about when its figures were
    measured; a value dated by its own publication would be wrong by up
    to a year. The wording gate is what prevents it: no period-end
    phrase, no metadata date, whatever the document happens to carry.
    """

    from fundscraper.field_definitions import PERIOD_END_REFERENCE_MARKERS, refers_to_period_end

    for wording in (
        "datum zverejneni 30.4.2025",
        "published on 30 april 2025",
        "vyrocni zprava 2024",
    ):
        assert not refers_to_period_end(wording), wording

    assert all("zverejn" not in marker for marker in PERIOD_END_REFERENCE_MARKERS)
    assert all("publish" not in marker for marker in PERIOD_END_REFERENCE_MARKERS)
