from __future__ import annotations

from fundscraper.field_definitions import (
    classify_annualization,
    classify_capital_metric,
    classify_fee_tier_basis,
    classify_historical_value,
    classify_party_role,
    classify_return_series,
    classify_return_type,
    clean_party_name,
    declared_frequency,
    frequency_from_gap_days,
    is_generic_company_name,
    is_negotiated_fee,
    is_news_article_path,
    is_news_path,
    months_from_period,
    normalize_ico,
    share_class_code,
    states_no_published_return,
)
from fundscraper.html_discovery import normalize_search_text
from fundscraper.output_models import (
    Annualization,
    AumMetricType,
    FeeTierBasis,
    HistoricalValueType,
    PartyRole,
    ReturnSeriesType,
    ReturnType,
    SeriesFrequency,
)


def test_recognises_both_party_roles_in_one_sentence() -> None:
    normalized = normalize_search_text(
        "Obhospodařovatelem a administrátorem Podfondu je CODYA investiční společnost, a.s."
    )

    assert classify_party_role(normalized) == (
        PartyRole.MANAGER,
        PartyRole.ADMINISTRATOR,
    )


def test_recognises_the_czech_synonyms_of_a_manager() -> None:
    for wording in (
        "Správce fondu",
        "Obhospodařovatel fondu",
        "Investment manager",
    ):
        assert PartyRole.MANAGER in classify_party_role(normalize_search_text(wording))


def test_strips_the_sentence_that_introduced_a_company() -> None:
    assert clean_party_name("em Společnosti je CODYA investiční společnost, a.s.") == (
        "CODYA investiční společnost, a.s."
    )


def test_drops_a_date_that_a_connective_hid_in_front_of_the_company() -> None:
    """The nine contaminated party values all read "je pocinaje <date> AVANT ...".

    The date was stripped before the leading connective was, so once "pocinaje" was gone the
    date stood at the front of the name and nothing removed it any more.
    """

    assert clean_party_name("počínaje 10. 05. 2018 AVANT investiční společnost, a.s.") == (
        "AVANT investiční společnost, a.s."
    )

    assert clean_party_name("počínaje 21.12.2017 AVANT investiční společnost, a. s.") == (
        "AVANT investiční společnost, a. s."
    )

    assert clean_party_name("s účinností od 4. 10. 2021 AVANT investiční společnost, a.s.") == (
        "AVANT investiční společnost, a.s."
    )

    assert clean_party_name("počínaje 29. ledna 2021 AVANT investiční společnost, a.s.") == (
        "AVANT investiční společnost, a.s."
    )


def test_keeps_the_digits_of_a_company_named_with_them() -> None:
    """The neighbouring value that must keep passing: a name that opens with digits."""

    assert clean_party_name("3M FUND MSI SICAV a.s.") == "3M FUND MSI SICAV a.s."

    assert clean_party_name("4stavební a.s.") == "4stavební a.s."

    assert clean_party_name("2N TELEKOMUNIKACE a.s.") == "2N TELEKOMUNIKACE a.s."

    # a bare year in front of a name is a date; a year inside one is not
    assert clean_party_name("2021 AVANT investiční společnost, a.s.") == (
        "AVANT investiční společnost, a.s."
    )


def test_refuses_a_name_that_is_only_a_legal_form() -> None:
    assert clean_party_name("Investiční společnost") is None

    assert clean_party_name("SICAV, a. s.") is None

    assert is_generic_company_name("investiční společnost")


def test_refuses_a_sentence_that_ends_in_a_legal_form() -> None:
    assert (
        clean_party_name(
            "AMISTA IS se na základě ust. § 642 odst. 3 ZISIF považuje za investiční společnost"
        )
        is None
    )

    assert clean_party_name("Statutární orgán Fondu, AVANT IS, je investiční společnost") is None


def test_reads_a_registration_number_printed_in_groups() -> None:
    assert normalize_ico("068 76 897") == "06876897"

    assert normalize_ico("1234") is None


def test_separates_the_capital_metrics_of_one_statement() -> None:
    assert (
        classify_capital_metric(normalize_search_text("Zapisovaný základní kapitál Fondu"))
        is AumMetricType.REGISTERED_CAPITAL
    )

    assert (
        classify_capital_metric(normalize_search_text("Výše fondového kapitálu"))
        is AumMetricType.FUND_CAPITAL
    )

    assert (
        classify_capital_metric(normalize_search_text("Minimální výše kapitálu dle ZISIF"))
        is AumMetricType.STATUTORY_MINIMUM_CAPITAL
    )

    assert (
        classify_capital_metric(normalize_search_text("Aktiva ve správě skupiny"))
        is AumMetricType.MANAGER_AUM
    )


def test_separates_the_reported_return_periods() -> None:
    cases = {
        "Výkonnost fondu v jednotlivých letech": ReturnSeriesType.CALENDAR_YEAR,
        "Kumulativní výkonnost": ReturnSeriesType.CUMULATIVE,
        "YTD": ReturnSeriesType.YEAR_TO_DATE,
        "Výnos za posledních 12 měsíců": ReturnSeriesType.ROLLING_12M,
        "Průměrné roční zhodnocení": ReturnSeriesType.ANNUALIZED_MULTI_YEAR,
        "Nepříznivý scénář": ReturnSeriesType.KID_SCENARIO,
    }

    for wording, expected in cases.items():
        assert classify_return_series(normalize_search_text(wording)) is expected


def test_separates_the_measured_historical_quantities() -> None:
    cases = {
        "Hodnota čistých aktiv na akcii": HistoricalValueType.NAV_PER_SHARE,
        "Aktuální hodnota investiční akcie": HistoricalValueType.INVESTMENT_SHARE_VALUE,
        "Fondový kapitál": HistoricalValueType.FUND_CAPITAL,
        "Čistá aktiva": HistoricalValueType.FUND_NET_ASSETS,
        "Assets under management": HistoricalValueType.AUM,
    }

    for wording, expected in cases.items():
        assert classify_historical_value(normalize_search_text(wording)) is expected


def test_separates_the_published_return_concepts() -> None:
    cases = {
        "Hurdle rate 7 % ročně": ReturnType.HURDLE,
        "Přednostní výnos do 7 % p.a.": ReturnType.PREFERRED,
        "Minimální zhodnocení PIA 6,1 % p.a.": ReturnType.GUARANTEED_MINIMUM,
        "Očekávaný výnos 8 - 9 % p.a.": ReturnType.EXPECTED,
        "Cílový výnos p.a. 6 %": ReturnType.TARGET,
    }

    for wording, expected in cases.items():
        assert classify_return_type(normalize_search_text(wording)) is expected


def test_recognises_that_no_return_is_published() -> None:
    assert states_no_published_return(normalize_search_text("Cílový výnos není stanoven."))

    assert not states_no_published_return(normalize_search_text("Cílový výnos je 6 % p.a."))


def test_reads_the_annualization_of_a_rate() -> None:
    assert classify_annualization(normalize_search_text("8 % p.a.")) is Annualization.PER_ANNUM

    assert (
        classify_annualization(normalize_search_text("Kumulativní výkonnost 26,62 %"))
        is Annualization.CUMULATIVE
    )

    assert classify_annualization(normalize_search_text("8 %")) is Annualization.UNKNOWN


def test_reads_the_frequency_of_a_series() -> None:
    assert (
        declared_frequency(normalize_search_text("Oceňování probíhá měsíčně"))
        is SeriesFrequency.MONTHLY
    )

    assert frequency_from_gap_days(1) is SeriesFrequency.DAILY

    assert frequency_from_gap_days(31) is SeriesFrequency.MONTHLY

    assert frequency_from_gap_days(92) is SeriesFrequency.QUARTERLY

    assert frequency_from_gap_days(365) is SeriesFrequency.ANNUAL

    assert frequency_from_gap_days(900) is SeriesFrequency.IRREGULAR


def test_reads_a_share_class_from_its_own_wording() -> None:
    assert share_class_code("Třída PIA: 1,3820 CZK") == "PIA"

    assert share_class_code("Investiční akcie třídy A") == "A"

    assert share_class_code("Hodnota investiční akcie VIA") == "VIA"

    assert share_class_code("Fondový kapitál celkem") is None


def test_reads_the_class_behind_a_declined_label_not_its_ending() -> None:
    # The statute of 3M FUND MSI SICAV a.s. writes the class in the
    # accusative. The ending "u" of "tridu" was reported as a class of
    # its own, which put a share class named "U" into the output.
    quote = "b) Minimální zajištěné zhodnocení pro třídu A činí 2 % p. a. na Referenčním období."

    assert share_class_code(quote) == "A"


def test_reads_every_declension_of_the_class_label() -> None:
    cases = {
        "Třída A": "A",
        "ve třídě B": "B",
        "pro třídu A": "A",
        "hodnota třídy Z": "Z",
        "investiční akcie třídy PIA": "PIA",
        "Investiční akcie třídy PRIA": "PRIA",
        "fondový kapitál třídy IAA": "IAA",
        "class B": "B",
        "share class A": "A",
        "třída b": "B",
    }

    for wording, expected in cases.items():
        assert share_class_code(wording) == expected, wording


def test_picks_the_first_class_when_a_row_names_several() -> None:
    # Both codes appear without the word "trida", so the answer comes
    # from the bare-code list. Reading that set in its own order returned
    # PIA or VIA depending on the hash seed of the process.
    quote = (
        "Prioritní investiční akcie PIA CZK x 10 %\n"
        "Výkonnostní investiční akcie VIA CZK x x\n"
        "V období do 31.12.2024 se přednostní výnos PIA navyšuje na 7 % p.a."
    )

    assert share_class_code(quote) == "PIA"

    assert share_class_code("VIA a PIA") == "VIA"


def test_refuses_an_ordinary_word_standing_after_the_label() -> None:
    for wording in (
        "Výstupní poplatek třídy dle statutu fondu",
        "hodnota třídy podle ceníku",
        "Fondový kapitál celkem",
    ):
        assert share_class_code(wording) is None, wording


def test_converts_a_holding_period_into_months() -> None:
    assert months_from_period(count=2, unit="let") == 24

    assert months_from_period(count=24, unit="mesicu") == 24


def test_reads_what_decides_a_fee_tier() -> None:
    assert (
        classify_fee_tier_basis(normalize_search_text("Výstupní poplatek do 2 let"))
        is FeeTierBasis.HOLDING_PERIOD
    )

    assert (
        classify_fee_tier_basis(normalize_search_text("Dle podmínek distributora"))
        is FeeTierBasis.DISTRIBUTOR
    )

    assert (
        classify_fee_tier_basis(normalize_search_text("Vstupní poplatek třídy PIA"))
        is FeeTierBasis.SHARE_CLASS
    )


def test_recognises_a_negotiated_fee() -> None:
    assert is_negotiated_fee(normalize_search_text("Vstupní poplatek dle dohody"))

    assert not is_negotiated_fee(normalize_search_text("Vstupní poplatek 3 %"))


def test_separates_a_news_listing_from_an_article() -> None:
    assert is_news_path("/aktuality/")

    assert not is_news_article_path("/aktuality/")

    assert is_news_article_path("/aktuality/vyrocni-zprava-2024")

    assert not is_news_path("/dokumenty/statut.pdf")
