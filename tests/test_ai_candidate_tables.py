"""
Which parsed table an AI completion candidate is allowed to read.

Every fixture here is the shape of a real document. The SNP INVEST case
is the one that forced this scorer to exist: the builder read a paragraph
the PDF parser had shredded into cells on an early page, and never
reached the indicator table whose header names the previous accounting
period *before* the current one. Reading that table by column position
inverts every value in it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ai_candidate_tables.py"

_specification = importlib.util.spec_from_file_location("ai_candidate_tables", MODULE_PATH)

assert _specification is not None
assert _specification.loader is not None

tables = importlib.util.module_from_spec(_specification)

sys.modules[_specification.name] = tables

_specification.loader.exec_module(tables)


# The paragraph the parser shredded into cells, verbatim in shape.
SNP_PROSE = [
    ["d", "e investovat zejména do majetkových účastí v n", "e", "m", "ovitostních společnostech"],
    ["y", "s tím, že výnosy investic Fondu budou primárně dividendy a úroky z poskytnutých", "o a"],
    ["", "Fond bude rovněž odkupovat investiční akcie za aktuální hodnotu vyhlašovanou", "v"],
    ["a", "čistá aktiva Fondu se mohou v čase měnit podle vývoje hodnoty portfolia", "d"],
]

# The real indicator table, page 10.
SNP_TABLE = [
    [
        "Ukazatel",
        "Stav k poslednímu dni předcházejícího účetního období",
        "Stav k poslednímu dni Účetního období",
        "Změna v %",
    ],
    ["NAV Fondu", "221 913 tis. Kč", "261 613 tis. Kč", "17,89 %"],
    ["Čistý zisk", "90 529 tis. Kč", "41 801 tis. Kč", "-53,83 %"],
    ["Hodnota prioritní akcie (v Kč)", "31 692,68 Kč", "37 362,60 Kč", "17,89 %"],
]

NAV_PROBE = re.compile(r"nav fondu|cista aktiva|fondovy kapital")

SHARE_PROBE = re.compile(r"hodnota prioritni akcie|hodnota investicni akcie")


def test_the_real_indicator_table_outranks_the_prose_pseudo_table() -> None:
    """The regression: the builder used to take the paragraph."""

    chosen = tables.select_best_table(
        tables=[SNP_PROSE, SNP_TABLE],
        field="aum_history",
        probe=NAV_PROBE,
    )

    assert chosen is not None

    index, assessment = chosen

    assert index == 1, "the prose pseudo-table was selected again"
    assert assessment.target_index == 1
    assert not assessment.is_prose


def test_the_prose_pseudo_table_scores_below_zero() -> None:
    assessment = tables.assess_table(rows=SNP_PROSE, field="aum_history", probe=NAV_PROBE)

    assert assessment.is_prose
    assert assessment.score < 0


def test_the_selected_table_preserves_the_inverted_column_order() -> None:
    """
    The header must reach the reader intact.

    This table names the *previous* period first. A builder that hands
    over rows without that header lets the values be read backwards, so
    the header is what the candidate is really for.
    """

    _, assessment = tables.select_best_table(
        tables=[SNP_PROSE, SNP_TABLE],
        field="historical_values",
        probe=SHARE_PROBE,
    )

    header = [row for row in SNP_TABLE[: assessment.target_index] if tables.is_header_row(row)]

    assert header, "no header row was identified"

    text = " ".join(str(c) for row in header for c in row)

    previous = text.index("předcházejícího")
    current = text.index("Stav k poslednímu dni Účetního")

    assert previous < current, "the header no longer shows the previous period first"

    # And the row the reader gets carries both figures, in that order.
    target = SNP_TABLE[assessment.target_index]

    assert "31 692,68" in target[1]
    assert "37 362,60" in target[2]


def test_the_nav_row_is_located_with_its_units() -> None:
    _, assessment = tables.select_best_table(
        tables=[SNP_PROSE, SNP_TABLE],
        field="aum_history",
        probe=NAV_PROBE,
    )

    target = SNP_TABLE[assessment.target_index]

    assert "221 913 tis. Kč" in target
    assert "261 613 tis. Kč" in target

    assert tables.context_is_complete(
        rows=SNP_TABLE,
        assessment=assessment,
        field="aum_history",
    )


def test_a_table_with_header_and_units_outranks_a_keyword_only_table() -> None:
    keyword_only = [
        ["Fondový kapitál je vymezen statutem", "", ""],
        ["hodnota", "", "12"],
    ]

    chosen = tables.select_best_table(
        tables=[keyword_only, SNP_TABLE],
        field="aum_history",
        probe=NAV_PROBE,
    )

    assert chosen is not None
    assert chosen[0] == 1


def test_a_stray_year_and_currency_do_not_make_prose_complete() -> None:
    """
    Context has to belong to the target row, not merely to the page.

    This is the Wine Management rule stated as a test: a unit somewhere
    else in the document is not the unit of this table.
    """

    strays = [
        ["V roce 2024 fond pokračoval v investiční strategii uvedené ve statutu podfondu"],
        ["Čistá aktiva připadající držitelům investičních akcií byla vykázána v Kč"],
        ["Zůstatek", "268 601"],
    ]

    assessment = tables.assess_table(
        rows=strays,
        field="aum_history",
        probe=re.compile(r"cista aktiva|zustatek"),
    )

    assert not tables.context_is_complete(
        rows=strays,
        assessment=assessment,
        field="aum_history",
    )


def test_a_dateless_table_is_not_complete_even_when_well_formed() -> None:
    """Wine Management: column order and rows are fine, the unit is not there."""

    dateless = [
        ["", "Běžné účetní období", "Minulé účetní období"],
        ["Zůstatek k 31.12.2025", "268 601", "343 827"],
    ]

    assessment = tables.assess_table(
        rows=dateless,
        field="aum_history",
        probe=re.compile(r"zustatek"),
    )

    # The header names periods, so the period test passes; the unit does
    # not appear anywhere in the table, so the candidate stays incomplete.
    assert not tables.context_is_complete(
        rows=dateless,
        assessment=assessment,
        field="aum_history",
    )


def test_no_table_is_chosen_when_every_match_is_structurally_weak() -> None:
    weak = [
        [["Fond investuje do nemovitostí a jeho čistá aktiva se mohou měnit v čase podle"]],
        [["hodnota"], ["čistá aktiva"]],
    ]

    assert (
        tables.select_best_table(
            tables=weak,
            field="aum_history",
            probe=re.compile(r"cista aktiva"),
        )
        is None
    )


def test_a_fee_schedule_needs_a_rate_not_a_period() -> None:
    """A statute states the fee in force; it has no reporting period."""

    schedule = [
        ["Poplatky a náklady", "", ""],
        ["Vstupní poplatek", "5 % max z hodnoty vydávaných investičních akcií", ""],
        ["Výstupní poplatek", "0 % max z hodnoty odkupovaných investičních akcií", ""],
    ]

    assessment = tables.assess_table(
        rows=schedule,
        field="fees",
        probe=re.compile(r"vstupni poplatek"),
    )

    assert tables.context_is_complete(rows=schedule, assessment=assessment, field="fees")


# The chart caption on page 59 of the same fund's annual report, verbatim.
# The parser shaped a value axis and a one-sentence caption into a grid,
# then cut the caption's first word in half across two rows.
SNP_CHART_CAPTION = [
    ["2,3000 Kč", "", "", "", "", "", "", "", "", ""],
    ["2,2000 Kč", "", "", "", "", "", "", "", "", ""],
    ["2,1000 Kč 2,0000 Kč", "", "", "", "", "", "", "", "", ""],
    ["Hodn", "", "", "", "", "", "", "", "", ""],
    [
        "",
        "ota Výkonnostní investiční akcie k 31. 12. 2",
        "",
        "",
        "",
        "025 činila 2,6605 Kč.",
        "",
        "",
        "",
        "",
    ],
]

RETURN_PROBE = re.compile(r"vykonnost|zhodnoceni|vynos fondu")


def test_the_chart_caption_is_not_an_actionable_annual_returns_table() -> None:
    """
    The Batch 8 regression: this scored 16 and read as complete context.

    Its cells are short and it holds numbers, so neither the average
    cell length nor the numeric test caught it. What gives it away is
    that no row names a column and the target row is half a sentence.
    """

    assessment = tables.assess_table(
        rows=SNP_CHART_CAPTION,
        field="annual_returns",
        probe=RETURN_PROBE,
    )

    assert assessment.score < tables.MINIMUM_TABLE_SCORE
    assert not tables.context_is_complete(
        rows=SNP_CHART_CAPTION,
        assessment=assessment,
        field="annual_returns",
    )

    assert (
        tables.select_best_table(
            tables=[SNP_CHART_CAPTION],
            field="annual_returns",
            probe=RETURN_PROBE,
        )
        is None
    )


def test_a_truncated_single_cell_is_not_a_column_header() -> None:
    """ "Hodn" named no column; neither did the axis label above it."""

    assert not tables.is_header_row(["Hodn", "", "", "", ""])
    assert not tables.is_header_row(["2,2000 Kč", "", "", "", ""])

    # A decimal fraction is not a year. This is why the axis label used
    # to earn the "header carries dates" bonus.
    assert not tables.YEAR_PATTERN.search("2,2000 Kč")
    assert tables.YEAR_PATTERN.search("31. 12. 2025")

    # Two named columns are still a header.
    assert tables.is_header_row(["Údaje k datu", "31. října 2025"])


def test_a_split_sentence_is_not_a_data_row() -> None:
    assert tables.looks_like_sentence_fragment(
        ["", "ota Výkonnostní investiční akcie k 31. 12. 2", "", "025 činila 2,6605 Kč."]
    )

    # The neighbouring case that must keep passing: a stray glyph the
    # parser left in front of a real value is one word, not a sentence.
    assert not tables.looks_like_sentence_fragment(
        ["Hodnota investiční akcie", "e 1,0331 Kč", "", "", "1,1227 Kč", "8,67 %"]
    )


# The VALOUR indicator table: column order is sound and the change
# column is a per-cent, but the share value's own currency is nowhere.
VALOUR_UNITLESS = [
    [
        "Ukazatel",
        "",
        "",
        "",
        "Stav k poslednímu",
        "",
        "Stav k poslednímu dni Účetního období",
        "",
        "",
        "Změna v %",
    ],
    ["", "", "", "", "dni předcházejícího", "", "", "", "", ""],
    ["Čistý zisk", "", "", "27 751", "", "", "40 538", "", "", "46,08"],
    ["Hodnota investiční akcie", "", "", "2,5656", "", "", "2,7573", "", "", "7,48"],
]


def test_a_per_cent_column_does_not_supply_the_currency_of_a_share_value() -> None:
    """
    Batch 8 rank 11: complete context on a table that never states Kč.

    The "Změna v %" column satisfied the old unit test, so a per-share
    value with no currency anywhere read as ready to extract.
    """

    assessment = tables.assess_table(
        rows=VALOUR_UNITLESS,
        field="historical_values",
        probe=re.compile(r"hodnota investicni akcie"),
    )

    assert assessment.target_index == 3

    assert not tables.context_is_complete(
        rows=VALOUR_UNITLESS,
        assessment=assessment,
        field="historical_values",
    )

    # The same table with the currency stated does qualify, so the rule
    # rejects the missing unit rather than the table's shape.
    with_currency = [
        *VALOUR_UNITLESS[:3],
        ["Hodnota investiční akcie", "", "", "2,5656 Kč", "", "", "2,7573 Kč", "", "", "7,48"],
    ]

    restated = tables.assess_table(
        rows=with_currency,
        field="historical_values",
        probe=re.compile(r"hodnota investicni akcie"),
    )

    assert tables.context_is_complete(
        rows=with_currency,
        assessment=restated,
        field="historical_values",
    )


def test_the_unit_a_field_needs_depends_on_what_it_measures() -> None:
    """A rate needs a per-cent; an amount needs a currency."""

    assert tables.unit_is_sufficient(field="annual_returns", text="zmena v %")
    assert not tables.unit_is_sufficient(field="annual_returns", text="v kc")

    for amount_field in ("aum_history", "assets_under_management", "historical_values"):
        assert not tables.unit_is_sufficient(field=amount_field, text="zmena v %")
        assert tables.unit_is_sufficient(field=amount_field, text="v tis. kc")

    # A fee is quoted either way, so either unit carries it.
    assert tables.unit_is_sufficient(field="fees", text="naklady na vstup 3 %")
    assert tables.unit_is_sufficient(field="fees", text="naklady na vstup 0 eur")


# REALIA FUND's indicator table, verbatim - including the way the parser
# wraps the label, leaving "Hodnota výkonnostní" in the row that holds the
# figures and "investiční akcie (VIA)" on the next one. That wrap is why
# the exclusion cannot depend on the word "akcie" being present.
REALIA_SHARE_VALUES = [
    [
        "Ukazatel",
        "Stav k poslednímu dni předcházejícího účetního období",
        "Stav k poslednímu dni Účetního období",
        "Změna v %",
    ],
    ["Čistý zisk", "148 315 tis. Kč", "163 615 tis. Kč", "10,32"],
    ["Hodnota výkonnostní", "2,1874 Kč", "2,5960 Kč", "18,68"],
    ["investiční akcie (VIA)"],
    ["Hodnota prioritní", "1,4460 Kč", "1,5787 Kč", "9,18"],
    ["investiční akcie I (PIA I)"],
]


def test_a_performance_share_class_is_not_an_annual_return() -> None:
    """
    The Batch 9 regression: ten of twelve candidates were this collision.

    "Vykonnostni investicni akcie" matched the annual_returns probe on
    "vykonnost", and the row it named held 2,5960 Kc - a per-share value.
    Reading the neighbouring 18,68 as the year's return would turn a
    per-share value into a performance figure.
    """

    assessment = tables.assess_table(
        rows=REALIA_SHARE_VALUES,
        field="annual_returns",
        probe=tables.FIELD_PROBES["annual_returns"],
    )

    assert assessment.target_index is None, "the share-class row was taken as a return again"


def test_the_same_row_still_qualifies_as_a_historical_value() -> None:
    """The row is not discarded: it is per-share evidence, so it routes here."""

    assessment = tables.assess_table(
        rows=REALIA_SHARE_VALUES,
        field="historical_values",
        probe=tables.FIELD_PROBES["historical_values"],
    )

    assert assessment.target_index == 2

    target = REALIA_SHARE_VALUES[assessment.target_index]

    assert "2,1874 Kč" in target and "2,5960 Kč" in target

    # Currency and period both stand in the table, so it is actionable.
    assert tables.context_is_complete(
        rows=REALIA_SHARE_VALUES,
        assessment=assessment,
        field="historical_values",
    )


def test_a_stated_performance_figure_still_qualifies_as_an_annual_return() -> None:
    """The exclusion must not swallow the thing the field is for."""

    stated = [
        ["Ukazatel", "2024", "2025"],
        ["Výkonnost podfondu za rok", "6,1 %", "8,4 %"],
    ]

    assessment = tables.assess_table(
        rows=stated,
        field="annual_returns",
        probe=tables.FIELD_PROBES["annual_returns"],
    )

    assert assessment.target_index == 1

    assert tables.context_is_complete(
        rows=stated,
        assessment=assessment,
        field="annual_returns",
    )


def test_the_exclusion_reads_the_adjective_not_the_word_performance() -> None:
    probe = tables.FIELD_PROBES["annual_returns"]

    # Share-class names, in the declensions the reports actually use -
    # including the two where the parser left the word "akcie" on the
    # following row, and the one report that misspells the adjective.
    for wording in (
        "hodnota vykonnostni investicni akcie (via)",
        "hodnota vykonnostni akcie",
        "hodnota vykonnosti investicni akce (via)",
        "hodnota vykonnostni  1,0785 kc   1,1787 kc 9,29",
        "hodnota vykonnosti  2,2800 kc   6,2964 kc 176,16 %",
    ):
        assert not tables.is_probe_match(field="annual_returns", text=wording, probe=probe)

    # A performance *fee* is not a return either.
    assert not tables.is_probe_match(
        field="annual_returns",
        text="vykonnostni odmena: 30 % z vynosu nad 8 % p.a.",
        probe=probe,
    )

    # Real statements of performance, including one about the shares.
    for wording in (
        "vykonnost fondu za rok 2025 cinila 8,4 %",
        "vykonnost investicnich akcii za rok 2025",
        "zhodnoceni za posledni 3 roky",
    ):
        assert tables.is_probe_match(field="annual_returns", text=wording, probe=probe)

    # A row that names the class *and* states a return keeps the return.
    assert tables.is_probe_match(
        field="annual_returns",
        text="vykonnostni investicni akcie - zhodnoceni za rok 2025: 8,4 %",
        probe=probe,
    )


# ČCE (A)/(B) page 6, verbatim. The table parses perfectly; the probe
# lands on the column heading, and the values are underneath it.
CCE_QUARTERLY = [
    ["Datum", "Hodnota investiční akcie", "Objem fondového kapitálu"],
    ["31.03.2025", "1,6042", "267 368 462 CZK"],
    ["30.06.2025", "1,6641", "285 137 760 CZK"],
    ["30.09.2025", "1,7263", "299 859 305 CZK"],
    ["31.12.2025", "1,7661", "306 779 988 CZK"],
]

# Algorithmic SICAV page 1, verbatim.
ALGORITHMIC_RETURNS = [
    ["Rok", "Čistá výkonnost v Kč"],
    ["2022", "-31.39%"],
    ["2023", "40.95%"],
    ["2024", "12.15%"],
    ["2025", "5.75%"],
]

# Volarik Capital page 22, verbatim. Structurally identical to the
# Algorithmic table and semantically a different thing entirely.
VOLARIK_GUARANTEE = [
    ["V letech", "Minimální zajištěné zhodnocení"],
    ["2026", "8 % p.a."],
    ["2027 - 2031", "7,5 % p.a."],
    ["2032 a dále", "5 % p.a."],
]


def test_a_probe_on_the_capital_heading_reads_the_rows_beneath_it() -> None:
    """
    The Batch 12 regression: a clean table refused for want of a header.

    "Objem fondového kapitálu" is a column heading. Taking it as the
    target left row 0 with nothing above it, so the candidate was thrown
    out as missing_header while its four dated rows sat underneath.
    """

    assessment = tables.assess_table(
        rows=CCE_QUARTERLY,
        field="aum_history",
        probe=tables.FIELD_PROBES["aum_history"],
    )

    assert assessment.header_index == 0, "the heading was not recognised as a heading"
    assert assessment.target_index == 1, "the target did not move to the first data row"

    target = CCE_QUARTERLY[assessment.target_index]

    assert "267 368 462 CZK" in target
    assert not any("Objem" in str(cell) for cell in target), "the heading became the value row"

    # The heading names which column the probe asked about.
    assert 2 in assessment.column_indexes

    assert tables.context_is_complete(
        rows=CCE_QUARTERLY,
        assessment=assessment,
        field="aum_history",
    )


def test_the_share_value_heading_is_a_historical_value_not_an_amount() -> None:
    """Two headings, two fields, one table - each must keep its own column."""

    historical = tables.assess_table(
        rows=CCE_QUARTERLY,
        field="historical_values",
        probe=tables.FIELD_PROBES["historical_values"],
    )

    assert historical.header_index == 0
    assert historical.column_indexes == (1,), "share value must map to its own column"

    capital = tables.assess_table(
        rows=CCE_QUARTERLY,
        field="assets_under_management",
        probe=tables.FIELD_PROBES["assets_under_management"],
    )

    assert capital.column_indexes == (2,), "fund capital must map to its own column"

    # The per-share column is not the amount column.
    assert historical.column_indexes != capital.column_indexes


def test_a_year_and_return_heading_yields_dated_returns() -> None:
    assessment = tables.assess_table(
        rows=ALGORITHMIC_RETURNS,
        field="annual_returns",
        probe=tables.FIELD_PROBES["annual_returns"],
    )

    assert assessment.header_index == 0
    assert assessment.target_index == 1
    assert ALGORITHMIC_RETURNS[assessment.target_index] == ["2022", "-31.39%"]

    assert tables.context_is_complete(
        rows=ALGORITHMIC_RETURNS,
        assessment=assessment,
        field="annual_returns",
    )


def test_a_forward_looking_guarantee_is_recovered_but_not_a_realised_return() -> None:
    """
    Structure recovery must not decide semantics.

    Volarik's schedule has the same shape as a realised-return table and
    promises a minimum for years that have not happened yet. The builder
    is allowed to surface it; nothing here may mark it as a return.
    """

    assessment = tables.assess_table(
        rows=VOLARIK_GUARANTEE,
        field="annual_returns",
        probe=tables.FIELD_PROBES["annual_returns"],
    )

    assert assessment.header_index == 0
    assert assessment.target_index == 1

    heading = " ".join(VOLARIK_GUARANTEE[assessment.header_index])

    # The heading carries the words that make it a promise, and they
    # reach the reader intact so adjudication can refuse it.
    assert "Minimální" in heading and "zajištěné" in heading

    # And the years it names are ahead of the periods a report measures.
    assert VOLARIK_GUARANTEE[assessment.target_index][0] == "2026"


def test_a_fee_heading_reads_the_rows_beneath_it() -> None:
    schedule = [
        ["Typ poplatku", "Sazba"],
        ["Vstupní poplatek", "max. 3 %"],
        ["Výstupní poplatek", "0 %"],
    ]

    assessment = tables.assess_table(
        rows=schedule,
        field="fees",
        probe=tables.FIELD_PROBES["fees"],
    )

    assert assessment.target_index == 1
    assert schedule[assessment.target_index] == ["Vstupní poplatek", "max. 3 %"]

    assert tables.context_is_complete(rows=schedule, assessment=assessment, field="fees")


def test_a_data_row_match_is_left_exactly_where_it_was() -> None:
    """
    No duplicate path: an ordinary match must not be moved.

    The heading rule fires only when the matched row names columns and
    holds no figures, so the Batch 8-12 selections keep the same target.
    """

    assessment = tables.assess_table(
        rows=SNP_TABLE,
        field="aum_history",
        probe=NAV_PROBE,
    )

    assert assessment.header_index is None
    assert assessment.column_indexes == ()
    assert assessment.target_index == 1
    assert SNP_TABLE[assessment.target_index][0] == "NAV Fondu"

    # A heading that also carries figures is a data row, not a heading.
    mixed = [
        ["Fondový kapitál 2025", "460 307 862"],
        ["Počet akcií", "36 343 460"],
    ]

    assert (
        tables.heading_match(
            rows=mixed,
            index=0,
            field="aum_history",
            probe=tables.FIELD_PROBES["aum_history"],
        )
        is None
    )


def test_a_heading_with_no_data_rows_beneath_it_is_not_moved() -> None:
    orphan = [
        ["Datum", "Objem fondového kapitálu"],
        ["Údaje nejsou k dispozici"],
    ]

    assert (
        tables.heading_match(
            rows=orphan,
            index=0,
            field="aum_history",
            probe=tables.FIELD_PROBES["aum_history"],
        )
        is None
    )


# Every schedule below opens with "Zůstatek k ...". Only some of them are
# a movement of the fund's own capital. All shapes are verbatim.

EQUISOL_TAX = [
    ["tis. Kč", "Rezerva na daň z příjmů", "Splatná daň z příjmů", "Odložená daň", "Celkem"],
    ["Zůstatek k 1. lednu 2023", "0", "0", "0", "0"],
    ["Tvorba daně z příjmů v účetním období", "5 367", "0", "0", "5 367"],
    ["Zůstatek k 31. prosinci 2023", "5 367", "0", "0", "5 367"],
]

PRIME_IMMO_PROVISIONS = [
    [
        "tis. Kč",
        "Rezerva na opravy majetku",
        "Rezerva na daně",
        "Rezervy na rizika a ztráty",
        "Rezervy ostatní",
        "Opravné položky k pohledávkám",
    ],
    ["Zůstatek k 1. lednu 2024", "0", "67", "0", "0", "0"],
    ["Zvýšení", "0", "383", "0", "0", "0"],
    ["Zůstatek k 31. prosinci 2024", "0", "383", "0", "0", "0"],
]

CDP_EQUITY = [
    ["V tis. Kč", "Vlastní", "Emisní", "Rezerv", "Kapitál.", "Oceňovací", "Zisk", "Celkem"],
    ["akcie", "ážio", "fondy", "fondy", "rozdíly", "(ztráta)"],
    ["Zůstatek k 5.1.2024", "0", "0", "0", "0", "0", "0", "0"],
    ["Čistý zisk/ztráta za účetní období", "0", "0", "0", "0", "0", "31 158", "31 158"],
    ["Emise akcií", "0", "0", "0", "0", "0", "412 760", "412 760"],
    ["Zůstatek k 31.12.2024", "0", "0", "0", "0", "0", "443 918", "443 918"],
]

EXPANDIA_ASSETS = [
    ["tis. Kč", "Tuzemsko", "EU", "Ostatní", "Celkem"],
    ["Pohledávky za bankami", "1 313", "0", "0", "1 313"],
    ["Účasti s rozhodujícím vlivem", "474 365", "0", "0", "474 365"],
    ["Ostatní aktiva", "288", "0", "0", "288"],
    ["Zůstatek k 31.12.2024", "475 966", "0", "0", "475 966"],
]


def test_a_tax_schedule_is_not_fund_capital_history() -> None:
    """
    The Batch 13 regression: "zůstatek k" reached accounting notes.

    Nothing in this table is the fund's capital; it is a movement of its
    income-tax provisions, and it reached the aum_history pool purely on
    the words "Zůstatek k".
    """

    assessment = tables.assess_table(
        rows=EQUISOL_TAX,
        field="aum_history",
        probe=tables.FIELD_PROBES["aum_history"],
    )

    assert assessment.target_index is None, "a tax schedule was taken as capital history"


def test_a_provisions_schedule_is_not_fund_capital_history() -> None:
    assessment = tables.assess_table(
        rows=PRIME_IMMO_PROVISIONS,
        field="aum_history",
        probe=tables.FIELD_PROBES["aum_history"],
    )

    assert assessment.target_index is None


def test_an_equity_movement_schedule_is_kept() -> None:
    """The neighbouring case: same opening words, real capital beneath."""

    assessment = tables.assess_table(
        rows=CDP_EQUITY,
        field="aum_history",
        probe=tables.FIELD_PROBES["aum_history"],
    )

    assert assessment.target_index is not None
    assert CDP_EQUITY[assessment.target_index][0].startswith("Zůstatek k")

    # And the schedule reconciles, which is why it is worth keeping.
    assert 31_158 + 412_760 == 443_918


def test_an_asset_schedule_split_by_geography_is_kept() -> None:
    """
    A geography split of assets is still a total of assets.

    The columns name countries, but the rows name what is being counted,
    and the balance line totals them.
    """

    assessment = tables.assess_table(
        rows=EXPANDIA_ASSETS,
        field="aum_history",
        probe=tables.FIELD_PROBES["aum_history"],
    )

    assert assessment.target_index is not None
    assert EXPANDIA_ASSETS[assessment.target_index][-1] == "475 966"

    assert 1_313 + 474_365 + 288 == 475_966


def test_a_balance_row_that_names_capital_itself_needs_no_vouching() -> None:
    """A row standing on its own words is unaffected by the new rule."""

    standalone = [
        ["Ukazatel", "31.12.2025"],
        ["Zůstatek fondového kapitálu k 31.12.2025", "268 601 tis. Kč"],
    ]

    assert tables.balance_row_has_capital_subject(
        rows=standalone,
        text=tables.fold(" ".join(standalone[1])),
    )

    # And a row with no balance wording at all is never questioned.
    assert tables.balance_row_has_capital_subject(
        rows=[["Fondový kapitál Podfondu (Kč)", "460 307 862"]],
        text=tables.fold("Fondový kapitál Podfondu (Kč) 460 307 862"),
    )


def test_the_rule_only_applies_to_the_history_field() -> None:
    """
    assets_under_management never matched on "zůstatek k" in the first
    place, so its probe must be left alone.
    """

    assert not tables.FIELD_PROBES["assets_under_management"].search("zustatek k 31.12.2025")
