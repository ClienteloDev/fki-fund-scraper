"""
Regression tests for the numeric defects found by the output audit.

Every fixture reproduces text taken from the document that produced the
wrong value, so the tests fail again if a fix is reverted.
"""

from __future__ import annotations

from fundscraper.field_extraction import ExtractedFundFields, extract_fund_fields
from fundscraper.output_models import FieldStatus
from tests.test_field_extraction import create_extraction_document

FUND_NAME = "EXAMPLE fond SICAV, a.s."


def fields(
    text: str,
    *,
    url: str = "https://www.examplefond.cz/dokument.pdf",
    document_type: str = "factsheet",
) -> ExtractedFundFields:
    document = create_extraction_document(
        source_id=1,
        url=url,
        title="EXAMPLE fond SICAV, a.s.",
        text=f"EXAMPLE fond SICAV, a.s.\n{text}",
        document_type=document_type,
    )

    return extract_fund_fields(
        fund_name=FUND_NAME,
        documents=[document],
    )


def test_year_is_not_extracted_as_a_target_return() -> None:
    """From the EFKD brochure: a broken layout glued "2026" to a percent."""

    result = fields(
        "pocitano s rocnim zhodnocenim 6,5 %.\ncilovy rocni vynos 2026%",
    )

    assert result.target_return.status is not FieldStatus.FOUND


def test_past_performance_is_not_a_target_return() -> None:
    result = fields(
        "vykonnost fondu od zalozeni za poslednich 12 mesicu\ncilovy rocni vynos 23,43 %",
    )

    assert result.target_return.status is not FieldStatus.FOUND


def test_kid_performance_scenario_is_not_a_target_return() -> None:
    result = fields(
        "scenare vykonnosti\nprizniv scenar: cilovy vynos 22,3 %",
        document_type="priips_kid",
    )

    assert result.target_return.status is not FieldStatus.FOUND


def test_a_real_target_return_is_still_extracted() -> None:
    result = fields("Cilovy vynos fondu je 8 % p.a.")

    assert result.target_return.status is FieldStatus.FOUND
    assert result.target_return.value is not None
    assert result.target_return.value.value_percent_pa == 8


def test_declared_thousands_unit_is_applied_to_assets() -> None:
    """A Czech statement declares its unit once, in the table header."""

    result = fields(
        "rozvaha k 31. prosinci 2025 (v tis. kc)\ncista aktiva 17 000 kc k 31.12.2025",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is FieldStatus.FOUND
    assert result.assets_under_management.value is not None
    assert result.assets_under_management.value.amount == 17_000_000


def test_absolute_amount_is_not_rescaled() -> None:
    """The same report also states absolute amounts, which must not move."""

    result = fields(
        "fondovy kapital: 693 601 745 kc k 31.12.2025",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is FieldStatus.FOUND
    assert result.assets_under_management.value is not None
    assert result.assets_under_management.value.amount == 693_601_745


def test_share_price_is_not_a_minimum_investment() -> None:
    result = fields(
        "aktualni hodnota investicni akcie tridy A\nminimalni investice 1,1138 kc",
    )

    assert result.minimum_investment.status is not FieldStatus.FOUND


def test_fractional_amount_is_not_a_minimum_investment() -> None:
    result = fields("minimalni investice cini 1,1138 kc")

    assert result.minimum_investment.status is not FieldStatus.FOUND


def test_a_real_minimum_investment_is_still_extracted() -> None:
    result = fields("Minimalni investice cini 1 000 000 Kc.")

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 1_000_000


def test_low_but_genuine_minimum_investment_is_kept() -> None:
    """MINT rezidencni fond really does accept 200 CZK."""

    result = fields("minimalni jednorazova investice je 200 kc")

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 200


def test_fee_range_stores_its_maximum_not_its_lower_bound() -> None:
    """From the DOMOPLAN statute: "od 0 % do 6 %"."""

    result = fields(
        "Vstupni poplatek od 0 % do 6 % z objemu investice dle Smlouvy o investici",
        document_type="statute",
    )

    assert result.fees.status is FieldStatus.FOUND
    assert result.fees.value is not None

    entry = next(item for item in result.fees.value.items if item.type.value == "entry")

    assert entry.rate_percent == 6
    assert entry.maximum is True


def test_explicit_zero_fee_is_still_zero() -> None:
    result = fields(
        "Vstupni poplatek (prirazka) 0 %",
        document_type="statute",
    )

    assert result.fees.status is FieldStatus.FOUND
    assert result.fees.value is not None

    entry = next(item for item in result.fees.value.items if item.type.value == "entry")

    assert entry.rate_percent == 0


def test_assets_without_a_date_are_not_extracted() -> None:
    result = fields(
        "cista aktiva 500 000 000 kc",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is not FieldStatus.FOUND


def test_a_date_is_not_read_as_an_amount() -> None:
    """From the Trikaya report: "31.12.2020" became the amount 12.2020."""

    result = fields(
        "31.12.2021 31.12.2020\ntis. Kc\nCista aktiva pripadajici na drzitele investicnich akcii",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is not FieldStatus.FOUND


def test_thousands_unit_applies_to_a_real_amount() -> None:
    result = fields(
        "cista aktiva k 31.12.2025 (v tis. kc)\ncista aktiva 12 202 kc",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is FieldStatus.FOUND
    assert result.assets_under_management.value is not None
    assert result.assets_under_management.value.amount == 12_202_000


def test_unrelated_amount_near_an_assets_label_is_not_used() -> None:
    """From the BOHEMIA report: a withholding tax became the net assets."""

    result = fields(
        "Ostatni pasiva predstavuji srazkovou dan z dluhopisu 17 tis. Kc "
        "a zavazek za vyplatou kuponu dluhopisu k 31.12.2025.\n"
        "Cista aktiva",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is not FieldStatus.FOUND


def test_amount_stated_next_to_its_label_is_used() -> None:
    result = fields(
        "Fondovy kapital: 693 601 745 Kc k 31.12.2025",
        document_type="annual_report",
    )

    assert result.assets_under_management.status is FieldStatus.FOUND
    assert result.assets_under_management.value is not None
    assert result.assets_under_management.value.amount == 693_601_745


def test_abbreviated_maximum_marks_the_fee_as_a_cap() -> None:
    """From the Trikaya factsheet: "Vstupni poplatek max. 4 %"."""

    result = fields(
        "// Vstupni poplatek max. 4 %",
        document_type="factsheet",
    )

    assert result.fees.status is FieldStatus.FOUND
    assert result.fees.value is not None

    entry = next(item for item in result.fees.value.items if item.type.value == "entry")

    assert entry.rate_percent == 4
    assert entry.maximum is True


def test_minimum_investment_written_with_a_scale_word_is_read() -> None:
    """
    Fund websites state the subscription minimum as "1 mil. Kč".

    The assets pattern has read a scale word since the beginning; the
    minimum-investment pattern demanded the currency straight after the
    number, so every site writing the amount in words lost the field.
    """

    result = fields("Minimalni investice klienta 1 mil. Kc")

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 1_000_000


def test_decimal_minimum_investment_with_a_scale_word_is_read() -> None:
    """ "3,5 mil. Kč" is three and a half million, not three."""

    result = fields("Minimalni investice cini 3,5 mil. Kc")

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 3_500_000


def test_scale_word_does_not_rescale_an_absolute_minimum() -> None:
    """The amount written out in full must keep its own magnitude."""

    result = fields("Minimalni investice cini 1 000 000 Kc, tj. 1 mil. Kc")

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 1_000_000


def test_fractional_share_price_with_no_scale_word_is_still_refused() -> None:
    """The guard against per-share values must survive the scale word."""

    result = fields("minimalni investice cini 1,1138 kc")

    assert result.minimum_investment.status is not FieldStatus.FOUND


def _exit_tiers(text: str) -> list[tuple[int | None, int | None, float | None]]:
    result = fields(text, document_type="statute")

    assert result.fees.value is not None

    # Sorted by the period, not by where the line happened to state it:
    # the order of the clauses is incidental, the schedule is not.
    return sorted(
        (
            (tier.from_months, tier.to_months, tier.rate_percent)
            for item in result.fees.value.items
            for tier in item.tiers
        ),
        key=lambda tier: (tier[0] is None, tier[0]),
    )


def test_fee_tier_binds_the_rate_stated_before_its_period() -> None:
    """
    "0 % po 3 letech, 5 % do 3 let" states the rate ahead of its period.

    The tier reader only ever looked forward, so the period "po 3 letech"
    took the 5 % of the next clause and the schedule was published
    inverted: an investor leaving inside three years was quoted the rate
    of one leaving after them.
    """

    assert _exit_tiers("Vystupni poplatek 0 % po 3 letech, 5 % do 3 let") == [
        (0, 36, 5.0),
        (36, None, 0.0),
    ]


def test_fee_tier_still_binds_the_rate_stated_after_its_period() -> None:
    """The ordinary wording, which has always worked, must keep working."""

    assert _exit_tiers(
        "Vystupni poplatek do 1 roku - 10 %, od 1 do 2 let - 5 %, po 2 letech 0 %"
    ) == [
        (0, 12, 10.0),
        (12, 24, 5.0),
        (24, None, 0.0),
    ]


def test_fee_tier_does_not_take_a_rate_from_another_clause() -> None:
    """A clause without its own rate yields no tier rather than a borrowed one."""

    tiers = _exit_tiers("Vystupni poplatek 5 % do 3 let, dale dle ceniku po 3 letech")

    assert (36, None, 5.0) not in tiers
