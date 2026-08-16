"""
Field definitions of the extended fund data set.

Every concept added in schema version 3 is described here once: the Czech
and English wording that names it in a public document, and the rule that
turns that wording into an enum member of the output model.

The module holds no extraction logic and no I/O, so the same vocabulary
is used by the extraction, by the validation and by the audit. A label
that is only understood by one of them is how a statutory minimum capital
ends up stored as the assets of a fund.

All matching runs on text passed through
:func:`fundscraper.html_discovery.normalize_search_text`, which lowers the
case, removes diacritics and collapses whitespace. Every literal below is
therefore written without diacritics.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from fundscraper.output_models import (
    Annualization,
    AumMetricType,
    FeeTierBasis,
    HistoricalValueType,
    HorizonKind,
    PartyRole,
    ReturnSeriesType,
    ReturnType,
    SeriesFrequency,
)

# ---------------------------------------------------------------------------
# 1. Parties acting for the fund
# ---------------------------------------------------------------------------


# The wording that names a role. Longer labels come first inside a role so
# that "administrator fondu" is preferred over the bare "administrator",
# and the roles themselves are ordered from the most to the least specific.
#
# "investicni spolecnost" is deliberately absent here: it is part of the
# legal name of almost every Czech management company ("CODYA investicni
# spolecnost, a.s."), so treating it as a role label would mark the name
# itself as the label and read the following words as the company.
PARTY_ROLE_LABELS: Final[
    tuple[
        tuple[
            PartyRole,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        PartyRole.MANAGER,
        (
            "obhospodarovatelem a administratorem",
            "obhospodarovatel a administrator",
            "obhospodarovatele fondu",
            "obhospodarovatelem fondu",
            "obhospodarovatel fondu",
            "obhospodarovatel podfondu",
            "obhospodarovatelem podfondu",
            "obhospodarovatel",
            "obhospodarovatelem",
            "obhospodarovateli",
            "spravce fondu",
            "spravcem fondu",
            "spravcovska spolecnost",
            "manazer fondu",
            "investment manager",
            "fund manager",
            "management company",
            "asset manager",
            "portfolio manager",
        ),
    ),
    (
        PartyRole.ADMINISTRATOR,
        (
            "administrator fondu",
            "administratorem fondu",
            "administrator podfondu",
            "administratorem podfondu",
            "administrace fondu",
            "administrator",
            "administratorem",
            "administratora",
            "fund administrator",
        ),
    ),
    (
        PartyRole.DEPOSITARY,
        (
            "depozitar fondu",
            "depozitarem fondu",
            "depozitar",
            "depozitarem",
            "depositary",
            "custodian",
        ),
    ),
    (
        PartyRole.AUDITOR,
        (
            "auditor fondu",
            "auditorem fondu",
            "auditor",
            "auditorem",
            "statutory auditor",
        ),
    ),
)


# Labels that name the manager and the administrator in a single sentence.
# "Obhospodarovatelem a administratorem Podfondu je CODYA investicni
# spolecnost, a.s." states one company for both roles.
COMBINED_PARTY_LABELS: Final[tuple[str, ...]] = (
    "obhospodarovatelem a administratorem",
    "obhospodarovatel a administrator",
    "obhospodarovatelem a administratorem podfondu",
    "obhospodarovatel a administrator fondu",
    "administratorem a obhospodarovatelem",
    "administrator a obhospodarovatel",
)


# A Czech management company is an "investicni spolecnost". Matching that
# wording first keeps the manager apart from the fund itself, whose name
# ends in the same legal form ("... SICAV, a.s.").
MANAGEMENT_COMPANY_PATTERN: Final = re.compile(
    r"""
    (?P<name>
        [^\n:;()•\[\]]{2,70}?
        investi[čc]n[íi]\s+spole[čc]nost
        (?:\s*,?\s*a\.\s?s\.?)?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Any other Czech company: the name ends in its legal form.
COMPANY_NAME_PATTERN: Final = re.compile(
    r"""
    (?P<name>
        [^\n:;()•\[\]]{2,70}?
        (?:
            a\.\s?s\.
            |
            s\.\s?r\.\s?o\.
            |
            spol\.\s*s\s*r\.\s*o\.
            |
            SE
        )
    )
    (?![\w.])
    """,
    re.VERBOSE,
)


# Words that join a label to the company name. They are stripped from the
# front of a captured name, otherwise the stored manager reads
# "je CODYA investicni spolecnost".
PARTY_NAME_LEADING_NOISE: Final[tuple[str, ...]] = (
    "je",
    "jsou",
    "fondu",
    "fondem",
    "fond",
    "podfondu",
    "podfond",
    "spolecnosti",
    "spolecnost",
    "depozitarem",
    "depozitar",
    "smlouva",
    "smlouvu",
    "smlouvy",
    "rozhodnuti",
    "prijima",
    "povazuje",
    "cinnost",
    "ust",
    "dle",
    "podle",
    "kdyz",
    "jako",
    "a",
    "i",
    "s",
    "se",
    "na",
    "do",
    "od",
    "k",
    "ke",
    "v",
    "ve",
    "za",
    "pro",
    "the",
    "is",
    "of",
    "for",
    "and",
    "by",
    "with",
)


# Tokens that every Czech management company shares. A name built only
# from them is the legal form, not a company: "Investicni spolecnost"
# and "SICAV, a. s." name nobody.
GENERIC_COMPANY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "s",
        "as",
        "sro",
        "se",
        "spol",
        "o",
        "c",
        "p",
        "ocp",
        "investicni",
        "investment",
        "spolecnost",
        "spolecnosti",
        "sicav",
        "fond",
        "fondu",
        "fondy",
        "fund",
        "funds",
        "company",
        "management",
    }
)


# Wording that proves a capture is a sentence about a company rather
# than its name. A statute writes "AMISTA IS se na zaklade ust. § 642
# odst. 3 ZISIF povazuje za investicni spolecnost", which ends in the
# same words as a legal name and is not one.
COMPANY_SENTENCE_MARKERS: Final[tuple[str, ...]] = (
    "je",
    "jsou",
    "organ",
    "statutarni",
    "prijima",
    "povazuje",
    "vykonava",
    "poverila",
    "poveril",
    "uzavrela",
    "kdyz",
    "na zaklade",
    "odst",
    "zisif",
    "rozhodnuti",
    "cinnosti",
    "byla",
    "bude",
    "muze",
    "provadi",
    "zajistuje",
    "obhospodaruje",
    "administraci",
)


# The longest legal name of a Czech company, in words. Anything longer
# is a sentence that happens to end in a legal form.
MAXIMUM_COMPANY_NAME_WORDS: Final = 8


# Wording of a Czech company registration number, followed by eight
# digits that a PDF may print in groups ("IC: 068 76 897").
ICO_PATTERN: Final = re.compile(
    r"""
    (?:
        i[čc]o?
        |
        identifika[čc]n[íi]\s+[čc][íi]slo
        |
        company\s+(?:id|number)
    )
    \s*:?\s*
    (?P<ico>\d[\d\s]{6,12}\d)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# How far after its label the name of a party may stand. A statute writes
# the label and the company in one sentence; a marketing page puts the
# label on its own line and the company on the next one.
PARTY_LABEL_MAXIMUM_DISTANCE: Final = 120


# ---------------------------------------------------------------------------
# 2. Capital and assets-under-management metrics
# ---------------------------------------------------------------------------


# Which capital figure a label names. The order matters: a statement of a
# fund contains "zapisovany zakladni kapital" and "fondovy kapital" in the
# same paragraph, and only the most specific label may win.
# Wording that turns a stated number of years into a bound rather than a
# figure. "min. 3 roky" and "5 let a vice" both name a floor, and one
# delivered output reported the first as an exact horizon of three years,
# which tells an investor to plan for exactly what the fund calls the
# least it will accept.
HORIZON_MINIMUM_MARKERS: Final[tuple[str, ...]] = (
    "min.",
    "min ",
    "minimalne",
    "minimalni",
    "nejmene",
    "alespon",
    "a vice",
    "a delsi",
    "a dele",
    "or more",
    "at least",
    "minimum",
)


HORIZON_MAXIMUM_MARKERS: Final[tuple[str, ...]] = (
    "maximalne",
    "maximalni",
    "nejvyse",
    "at most",
    "maximum",
)


# A horizon written as a span. "bude se pohybovat v rozmezi 3 - 5 let"
# names both ends, and one delivered output kept only the upper one as
# an exact horizon.
HORIZON_RANGE_PATTERN: Final = re.compile(
    r"""
    (?P<minimum>\d{1,2}(?:[,.]\d+)?)
    \s*
    (?:-|az|do|to)
    \s*
    (?P<maximum>\d{1,2}(?:[,.]\d+)?)
    \s*
    (?:let|rok|roky|roku|years?)
    """,
    re.VERBOSE,
)


def states_a_horizon_range(
    normalized: str,
) -> bool:
    """Return whether a horizon is written as a span of years."""

    match = HORIZON_RANGE_PATTERN.search(normalized)

    if match is None:
        return False

    return match.group("minimum") != match.group("maximum")


def classify_horizon_kind(
    normalized: str,
) -> HorizonKind:
    """
    Return whether a stated horizon is a floor or the figure itself.

    ``HorizonKind`` has no ceiling, and adding one would change a string
    every consumer of the delivered file already reads. A horizon stated
    as a maximum is therefore left as it is here and reported by
    ``validate_investment_horizon`` instead, so the case is visible
    without the schema moving under anyone.
    """

    if any(marker in normalized for marker in HORIZON_MINIMUM_MARKERS):
        return HorizonKind.MINIMUM

    return HorizonKind.EXACT


# A return stated as a reference rate plus a spread. The spread on its
# own is not the return: "2TR + 1 % p.a." was delivered as a target of
# 1 % a year, which is neither what the fund promises nor a number an
# investor could act on. The reference rate has to be named for the
# figure to mean anything, so a window carrying one of these markers is
# not a fixed rate.
BENCHMARK_RETURN_MARKERS: Final[tuple[str, ...]] = (
    "2tr",
    "2t repo",
    "repo sazb",
    "repo rate",
    "pribor",
    "euribor",
    "wibor",
    "sofr",
    "sazby cnb",
    "sazbou cnb",
    "sazba cnb",
    "inflac",
    "inflation",
    "benchmark",
)


def states_benchmark_linked_return(
    normalized: str,
) -> bool:
    """Return whether a stated rate is a spread over a reference rate."""

    return any(marker in normalized for marker in BENCHMARK_RETURN_MARKERS)


# Wording that turns a capital label into one component of it. A Czech
# statute summary lists "z toho neinvesticni fondovy kapital: 100 000 Kc"
# beside "z toho investicni fondovy kapital: ...", and the first is the
# registered shell of the fund rather than what it holds. Five delivered
# funds carried that component as their assets: 100 000 CZK for VALOUR,
# 29 950 for VENDEAVOUR, 30 000 for SALUTEM, 34 000 for Safety Real and
# 66 720 000 for WF Group, against real capital of hundreds of millions.
#
# "neinvesticni fondovy kapital" also *contains* the label
# "investicni fondovy kapital", so a plain substring search reads the
# negated component as the fund's own capital.
CAPITAL_COMPONENT_QUALIFIERS: Final[tuple[str, ...]] = (
    "neinvesticni",
    "neinvesticniho",
)


# Wording that names a date without writing one. A statute summary says
# the capital is stated "k poslednimu dni ucetniho obdobi" and never
# repeats the day, so the figure is correct, dated, and invisible to a
# date pattern. The document's own reporting period supplies the day.
PERIOD_END_REFERENCE_MARKERS: Final[tuple[str, ...]] = (
    "k poslednimu dni ucetniho obdobi",
    "k poslednimu dni obdobi",
    "ke konci ucetniho obdobi",
    "k rozvahovemu dni",
    "k datu ucetni zaverky",
    "at the end of the accounting period",
    "as at the balance sheet date",
)


def refers_to_period_end(
    normalized: str,
) -> bool:
    """Return whether a value is dated by naming the reporting period."""

    return any(marker in normalized for marker in PERIOD_END_REFERENCE_MARKERS)


def label_is_negated(
    *,
    normalized: str,
    label_start: int,
) -> bool:
    """Return whether a capital label is qualified away just before it."""

    lead = normalized[max(0, label_start - 40) : label_start]

    return any(lead.rstrip().endswith(word) for word in CAPITAL_COMPONENT_QUALIFIERS)


CAPITAL_METRIC_LABELS: Final[
    tuple[
        tuple[
            AumMetricType,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        AumMetricType.STATUTORY_MINIMUM_CAPITAL,
        (
            "minimalni vyse kapitalu",
            "minimalni vysi kapitalu",
            "minimalni kapital",
            "minimalniho kapitalu",
            "statutory minimum capital",
            "minimum capital requirement",
        ),
    ),
    (
        AumMetricType.REGISTERED_CAPITAL,
        (
            "zapisovany zakladni kapital",
            "zapisovaneho zakladniho kapitalu",
            "zapsany zakladni kapital",
            "zapsaneho zakladniho kapitalu",
            "splaceny zakladni kapital",
            "zakladni kapital",
            "zakladniho kapitalu",
            "registered capital",
            "share capital",
            "subscribed capital",
        ),
    ),
    (
        AumMetricType.MANAGER_AUM,
        (
            "aktiva ve sprave skupiny",
            "majetek ve sprave skupiny",
            "objem majetku ve sprave spolecnosti",
            "celkovy objem aktiv ve sprave",
            "assets under management of the group",
            "total assets under management",
            "group assets under management",
        ),
    ),
    (
        AumMetricType.FUND_CAPITAL,
        (
            "investicni fondovy kapital",
            "fondovy kapital",
            "fondoveho kapitalu",
            "vyse fondoveho kapitalu",
            "fund capital",
        ),
    ),
    (
        AumMetricType.NAV,
        (
            "net asset value",
            "hodnota cistych aktiv",
            "cista hodnota aktiv",
            "nav",
        ),
    ),
    (
        AumMetricType.NET_ASSETS,
        (
            "cista aktiva",
            "cistych aktiv",
            "cisty obchodni majetek",
            "net assets",
        ),
    ),
    (
        AumMetricType.ASSETS_UNDER_MANAGEMENT,
        (
            "aktiva ve sprave",
            "majetek ve sprave",
            "assets under management",
            "aum",
        ),
    ),
    (
        AumMetricType.ASSETS_TOTAL,
        (
            "celkova aktiva",
            "aktiva celkem",
            "total assets",
            "hodnota majetku fondu",
            "majetek fondu",
            "fund assets",
        ),
    ),
    (
        AumMetricType.EQUITY,
        (
            "vlastni kapital",
            "vlastniho kapitalu",
            "equity",
        ),
    ),
)


CAPITAL_METRIC_LABELS_BY_METRIC: Final[dict[AumMetricType, tuple[str, ...]]] = {
    metric: labels for metric, labels in CAPITAL_METRIC_LABELS
}


# The labels the assets extractor may read a fund's own capital from.
#
# Derived from the shared table, but only from the metrics that name the
# fund's capital itself. Widening it to every fund-level metric was
# tried and reverted: it added "vlastni kapital" and "aktiva celkem",
# which stand in *any* company's balance sheet, and one manager's annual
# report then supplied its own equity of 9 912 654 Kc as the assets of a
# fund it administers. The narrowness of this list is load-bearing.
#
# Inflected forms matter: "vyse fondoveho kapitalu" is how a Czech
# statute summary states the headline figure, and the extractor's older
# private list knew only the nominative "fondovy kapital".
FUND_CAPITAL_READING_LABELS: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            *CAPITAL_METRIC_LABELS_BY_METRIC[AumMetricType.FUND_CAPITAL],
            *CAPITAL_METRIC_LABELS_BY_METRIC[AumMetricType.NAV],
            *CAPITAL_METRIC_LABELS_BY_METRIC[AumMetricType.NET_ASSETS],
            *CAPITAL_METRIC_LABELS_BY_METRIC[AumMetricType.ASSETS_UNDER_MANAGEMENT],
            "majetek fondu",
            "hodnota majetku fondu",
            "fund assets",
        },
        key=len,
        reverse=True,
    )
)


# Every capital label, longest first. Used when the metric of a value has
# to be read from the words standing directly in front of it.
CAPITAL_METRIC_BY_LABEL: Final[
    tuple[
        tuple[
            str,
            AumMetricType,
        ],
        ...,
    ]
] = tuple(
    sorted(
        ((label, metric) for metric, labels in CAPITAL_METRIC_LABELS for label in labels),
        key=lambda item: -len(item[0]),
    )
)


# Wording turning a total into a figure per single share. "Fondovy
# kapital na 1 akcii" is the value of one share, not the capital of the
# fund, and reading it as the latter reported a billion-crown fund as
# holding one crown.
PER_SHARE_MARKERS: Final[tuple[str, ...]] = (
    "na 1 akcii",
    "na jednu akcii",
    "na akcii",
    "na investicni akcii",
    "na podilovy list",
    "per share",
    "na akcii v",
)


def describes_value_per_share(
    normalized: str,
) -> bool:
    """Return whether a label states a value per single share."""

    return any(marker in normalized for marker in PER_SHARE_MARKERS)


# Wording proving a capital figure describes the manager, the group or
# every fund together. Such a value is never the assets of this fund.
MANAGER_LEVEL_CAPITAL_MARKERS: Final[tuple[str, ...]] = (
    "skupina spravuje",
    "spolecnost spravuje",
    "investicni spolecnost spravuje",
    "spravcovska spolecnost spravuje",
    "obhospodarovatel spravuje",
    "ve sprave skupiny",
    "ve sprave spolecnosti",
    "aktiva ve sprave skupiny",
    "majetek ve sprave skupiny",
    "celkovy objem aktiv ve sprave",
    "napric fondy",
    "vsechny fondy",
    "vsech fondu",
    "our funds",
    "the group has",
    "group manages",
    "company manages",
    "manager manages",
    "assets under management of the group",
    "total assets under management",
)


# Wording proving a figure is the statutory floor a fund has to reach,
# not what it holds. The KID of nearly every Czech fund repeats the
# 1 250 000 EUR threshold, which is why it needs its own rule.
STATUTORY_CAPITAL_MARKERS: Final[tuple[str, ...]] = (
    "nedosahne hranice",
    "nedosahne vyse",
    "musi dosahnout",
    "minimalni vyse kapitalu",
    "minimalni kapital",
    "dle ust. § 29",
    "dle ust. 29",
    "§ 29 odst",
    "zisif",
    "must reach",
    "minimum capital",
)


# The metrics that may never be reported as the assets of a fund.
NON_FUND_CAPITAL_METRICS: Final[frozenset[AumMetricType]] = frozenset(
    {
        AumMetricType.REGISTERED_CAPITAL,
        AumMetricType.STATUTORY_MINIMUM_CAPITAL,
        AumMetricType.MANAGER_AUM,
    }
)


# Every label naming a fund-level capital figure, taken from the shared
# table above so the assets extractor and the metric classifier cannot
# drift apart. They already had: the extractor's private list knew
# "fondovy kapital" but not the genitive "fondoveho kapitalu", so
# "vyse fondoveho kapitalu: 672 417 510 Kc" — the headline figure of
# every AVANT statute summary — was invisible to it while the classifier
# read it correctly.
FUND_LEVEL_CAPITAL_LABELS: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            label
            for metric, labels in CAPITAL_METRIC_LABELS
            if metric not in NON_FUND_CAPITAL_METRICS
            for label in labels
        },
        key=len,
        reverse=True,
    )
)


# ---------------------------------------------------------------------------
# 3. Annual returns
# ---------------------------------------------------------------------------


# Which period a reported performance covers. A cumulative or a scenario
# figure carries the same percent sign as a calendar-year return, so the
# series type has to be read from the wording around it.
RETURN_SERIES_LABELS: Final[
    tuple[
        tuple[
            ReturnSeriesType,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        ReturnSeriesType.KID_SCENARIO,
        (
            "scenar",
            "scenare",
            "stresovy",
            "neprizniv",
            "umerny",
            "prizniv",
            "performance scenario",
            "stress scenario",
            "unfavourable",
            "unfavorable",
            "moderate scenario",
            "favourable",
            "favorable",
        ),
    ),
    (
        ReturnSeriesType.ANNUALIZED_MULTI_YEAR,
        (
            "anualizovan",
            "annualised",
            "annualized",
            "prumerne rocni",
            "prumerny rocni",
            "average annual",
            "p.a. za poslednich",
            "rocne za poslednich",
        ),
    ),
    (
        ReturnSeriesType.CUMULATIVE,
        (
            "kumulativn",
            "cumulative",
            "od zalozeni",
            "od vzniku fondu",
            "since inception",
            "celkove zhodnoceni od",
            "za cele obdobi",
        ),
    ),
    (
        ReturnSeriesType.YEAR_TO_DATE,
        (
            "ytd",
            "od zacatku roku",
            "year to date",
            "year-to-date",
        ),
    ),
    (
        ReturnSeriesType.ROLLING_12M,
        (
            "za poslednich 12 mesicu",
            "poslednich 12 mesicu",
            "za posledni rok",
            "mezirocni vykonnost",
            "meziroc",
            "rolling 12",
            "last 12 months",
            "trailing 12",
            "12m",
        ),
    ),
    (
        ReturnSeriesType.CALENDAR_YEAR,
        (
            "vykonnost v jednotlivych letech",
            "vykonnost fondu v jednotlivych letech",
            "vykonnost v letech",
            "rocni vykonnost",
            "rocni vynos",
            "vykonnost za rok",
            "zhodnoceni za rok",
            "vysledky v jednotlivych letech",
            "annual performance",
            "annual return",
            "calendar year performance",
            "yearly performance",
        ),
    ),
)


# Wording that marks a percentage as an achieved result rather than a
# plan. Only such a percentage may become an annual return.
PAST_RETURN_MARKERS: Final[tuple[str, ...]] = (
    "vykonnost",
    "vynos",
    "zhodnoceni",
    "vysledek",
    "vysledky",
    "return",
    "performance",
    "yield",
)


# ---------------------------------------------------------------------------
# 4. Historical values
# ---------------------------------------------------------------------------


HISTORICAL_VALUE_LABELS: Final[
    tuple[
        tuple[
            HistoricalValueType,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        HistoricalValueType.NAV_PER_SHARE,
        (
            "hodnota cistych aktiv na akcii",
            "cista hodnota aktiv na akcii",
            "net asset value per share",
            "navps",
            "nav na akcii",
            "nav/akcie",
        ),
    ),
    (
        HistoricalValueType.INVESTMENT_SHARE_VALUE,
        (
            "aktualni hodnota investicni akcie",
            "hodnota investicni akcie",
            "hodnota jedne investicni akcie",
            "cena investicni akcie",
            "kurz investicni akcie",
            "hodnota akcie tridy",
            "investment share value",
            "share price",
        ),
    ),
    (
        HistoricalValueType.FUND_CAPITAL,
        (
            "fondovy kapital",
            "fondoveho kapitalu",
            "fund capital",
        ),
    ),
    (
        HistoricalValueType.FUND_NET_ASSETS,
        (
            "cista aktiva",
            "cistych aktiv",
            "net assets",
        ),
    ),
    (
        HistoricalValueType.AUM,
        (
            "aktiva ve sprave",
            "majetek ve sprave",
            "assets under management",
            "aum",
        ),
    ),
)


# Wording that states the frequency of a series outright.
SERIES_FREQUENCY_LABELS: Final[
    tuple[
        tuple[
            SeriesFrequency,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        SeriesFrequency.DAILY,
        (
            "denni",
            "denne",
            "kazdy pracovni den",
            "daily",
        ),
    ),
    (
        SeriesFrequency.MONTHLY,
        (
            "mesicni",
            "mesicne",
            "kazdy kalendarni mesic",
            "k poslednimu dni kalendarniho mesice",
            "monthly",
        ),
    ),
    (
        SeriesFrequency.QUARTERLY,
        (
            "ctvrtletni",
            "ctvrtletne",
            "kvartalni",
            "quarterly",
        ),
    ),
    (
        SeriesFrequency.ANNUAL,
        (
            "rocni",
            "rocne",
            "kazdorocne",
            "annual",
            "yearly",
        ),
    ),
)


# The longest gap in days a series of the given frequency may show. A
# monthly series skips a month now and then, so the bounds are generous;
# anything above the last one is reported as irregular.
FREQUENCY_MAXIMUM_GAP_DAYS: Final[
    tuple[
        tuple[
            int,
            SeriesFrequency,
        ],
        ...,
    ]
] = (
    (10, SeriesFrequency.DAILY),
    (45, SeriesFrequency.MONTHLY),
    (130, SeriesFrequency.QUARTERLY),
    (400, SeriesFrequency.ANNUAL),
)


# ---------------------------------------------------------------------------
# 5. Target return concepts
# ---------------------------------------------------------------------------


# Which return concept a percentage describes. A preferred return of a
# priority share class and a target return of the fund are different
# promises, and storing one as the other misstates what an investor gets.
RETURN_TYPE_LABELS: Final[
    tuple[
        tuple[
            ReturnType,
            tuple[str, ...],
        ],
        ...,
    ]
] = (
    (
        ReturnType.HURDLE,
        (
            "hurdle rate",
            "hurdle",
            "minimalni pozadovana vynosnost",
            "prekazkova sazba",
        ),
    ),
    (
        ReturnType.PREFERRED,
        (
            "prednostni vynos",
            "prednostniho vynosu",
            "prioritni vynos",
            "prioritniho vynosu",
            "preferencni vynos",
            "preferred return",
            "priority return",
        ),
    ),
    (
        ReturnType.GUARANTEED_MINIMUM,
        (
            "garantovany vynos",
            "garantovane zhodnoceni",
            "zaruceny vynos",
            "zarucene zhodnoceni",
            "minimalni zhodnoceni",
            "minimalni vynos",
            "minimalni planovane zhodnoceni",
            "guaranteed return",
            "guaranteed minimum",
            "minimum return",
        ),
    ),
    (
        ReturnType.EXPECTED,
        (
            "ocekavany vynos",
            "ocekavane zhodnoceni",
            "ocekavana vykonnost",
            "predpokladany vynos",
            "predpokladane zhodnoceni",
            "expected return",
            "expected performance",
            "anticipated return",
        ),
    ),
    (
        ReturnType.TARGET,
        (
            "cilovy vynos",
            "cilove zhodnoceni",
            "cileny vynos",
            "cilene zhodnoceni",
            "cilova vykonnost",
            "target return",
            "target yield",
            "targeted return",
        ),
    ),
)


# Wording stating that no target return is published. It is a result of
# its own: an investor learns that the fund does not promise a number,
# which is different from the extraction having failed.
NOT_PUBLISHED_MARKERS: Final[tuple[str, ...]] = (
    "cilovy vynos neni stanoven",
    "cilovy vynos nen",
    "vynos neni stanoven",
    "vynos neni garantovan",
    "nestanovuje cilovy vynos",
    "nezverejnuje",
    "neuvadi cilovy vynos",
    "not published",
    "not disclosed",
    "no target return",
)


# Wording that annualizes a rate.
PER_ANNUM_MARKERS: Final[tuple[str, ...]] = (
    "p.a.",
    "p. a.",
    "p.a",
    "rocne",
    "za rok",
    "per annum",
    "annually",
    "yearly",
    # A source states the period in the name of the figure as often as
    # it does in a suffix: "Target annual return 30%" and "rocni vynos
    # 6 %" are per annum and were classified as unknown. Only the
    # phrases are matched, never the bare word, so the heading of an
    # annual report never turns a return into a rate per annum.
    "annual return",
    "annual target",
    "annual yield",
    "annualis",
    "annualiz",
    "rocni vynos",
    "rocniho vynosu",
    "rocni zhodnoceni",
    "rocniho zhodnoceni",
)


CUMULATIVE_MARKERS: Final[tuple[str, ...]] = (
    "kumulativn",
    "cumulative",
    "celkem za obdobi",
    "za cele obdobi",
    "since inception",
    "od zalozeni",
)


PERIOD_MARKERS: Final[tuple[str, ...]] = (
    "za obdobi",
    "za uvedene obdobi",
    "over the period",
    "for the period",
)


# ---------------------------------------------------------------------------
# 6. Fee tiers
# ---------------------------------------------------------------------------


# A holding period written as an upper bound: "do 2 let", "do 24 mesicu",
# "within 2 years". Everything redeemed earlier pays this tier.
HOLDING_PERIOD_TO_PATTERN: Final = re.compile(
    r"""
    \b(?:do|behem|within|before)\s+
    (?P<count>\d{1,3})
    \s*
    (?P<unit>let|leta|roku|rok|rocich|mesicu|mesice|mesic|months?|years?)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# A holding period written as a range: "od 1 do 2 let".
HOLDING_PERIOD_RANGE_PATTERN: Final = re.compile(
    r"""
    \bod\s+
    (?P<from>\d{1,3})
    \s*
    (?:let|leta|roku|rok|mesicu|mesice|mesic)?
    \s+do\s+
    (?P<to>\d{1,3})
    \s*
    (?P<unit>let|leta|roku|rok|mesicu|mesice|mesic|months?|years?)
    """,
    re.IGNORECASE | re.VERBOSE,
)


# A holding period written as a lower bound: "po 24 mesicich", "po 3
# letech od upisu", "after 3 years", "nad 5 let".
HOLDING_PERIOD_FROM_PATTERN: Final = re.compile(
    r"""
    \b(?:po|nad|after|od\s+viceleteho)\s+
    (?P<count>\d{1,3})
    \s*
    (?P<unit>letech|let|leta|rocich|roku|mesicich|mesicu|mesici|months?|years?)
    """,
    re.IGNORECASE | re.VERBOSE,
)


MONTHS_PER_YEAR: Final = 12


# Units that count in years rather than months.
YEAR_UNITS: Final[frozenset[str]] = frozenset(
    {
        "let",
        "leta",
        "roku",
        "rok",
        "rocich",
        "letech",
        "year",
        "years",
    }
)


# A share class of a Czech fund: "trida PIA", "investicni akcie tridy A",
# "class A". The captured code identifies the tier the fee belongs to.
#
# The ending of the label has to be spelled out completely. Czech declines
# "trida" through -a, -e, -u and -y, and an ending the pattern does not
# know is left for the code group to read: "pro tridu A" reported the
# accusative "u" as the share class of the fund.
#
# A separator between the label and the code is required for the same
# reason. Without it any letter touching the label reads as a class.
#
# A code written in lower case is a single letter. Multi-letter classes
# (PIA, VIA, PRIA, IAA) are always capitalised in these documents, while a
# lower-case run of up to four letters matches ordinary words, which is
# how "tridy dle statutu" would report a class named "DLE".
#
# Only the label ignores case. A sentence opening with "Trida A" was
# missed entirely because the pattern is case sensitive, but the code
# group must stay so: the difference between "PIA" and an ordinary word
# is exactly its capitalisation.
SHARE_CLASS_PATTERN: Final = re.compile(
    r"""
    (?i:
        t[rř][íi]d[aeěuy]?
        (?:\s+investi[čc]n[íi]ch\s+akci[íi])?
        |
        class
        |
        share\s+class
    )
    [\s:]+
    (?P<code>[A-Z]{1,4}\d?|[a-z]\d?)
    \b
    """,
    re.VERBOSE,
)


# The class codes a Czech qualified investor fund uses on their own,
# without the word "trida" in front of them.
BARE_SHARE_CLASS_CODES: Final[frozenset[str]] = frozenset(
    {
        "PIA",
        "VIA",
        "DIA",
        "PRIA",
        "IAA",
        "IAB",
        "IAC",
        "IAD",
        "IAE",
        "IAF",
    }
)


# Wording showing that a fee is agreed rather than published, so the
# stated number is a bound of a range and not the rate an investor pays.
NEGOTIATED_FEE_MARKERS: Final[tuple[str, ...]] = (
    "dle dohody",
    "po dohode",
    "dle smlouvy",
    "individualne",
    "individualni",
    "dle ceniku",
    "dle podminek distributora",
    "dle distributora",
    "sjednan",
    "by agreement",
    "negotiable",
    "as agreed",
    "depending on the distributor",
)


DISTRIBUTOR_MARKERS: Final[tuple[str, ...]] = (
    "distributor",
    "distributora",
    "zprostredkovatel",
    "zprostredkovatele",
    "obchodnik",
    "poradce",
    "adviser",
    "advisor",
)


# ---------------------------------------------------------------------------
# 7. Fund news
# ---------------------------------------------------------------------------


# Path segments that mark a page or an article as fund news. Only the
# official fund website and the website of its manager are read for now,
# so a matching path is enough to recognise the section.
NEWS_PATH_SEGMENTS: Final[tuple[str, ...]] = (
    "aktuality",
    "aktualita",
    "novinky",
    "novinka",
    "blog",
    "news",
    "newsroom",
    "clanky",
    "clanek",
    "tiskove-zpravy",
    "tiskova-zprava",
    "press",
    "press-releases",
    "insights",
    "magazin",
    "reporting",
)


# Words of a navigation link that never introduce an article.
NEWS_LINK_NOISE: Final[tuple[str, ...]] = (
    "cookie",
    "gdpr",
    "ochrana osobnich udaju",
    "kontakt",
    "kontakty",
    "o nas",
    "prihlaseni",
    "login",
    "vice",
    "zobrazit vse",
    "vsechny aktuality",
    "nacist dalsi",
    "read more",
    "more",
    "next",
    "previous",
    "dalsi",
    "predchozi",
    "home",
    "uvod",
    "zpet na prehled",
    "zpet",
    "prehled",
    "vsechny clanky",
    "vsechny novinky",
    "cist dale",
    "cely clanek",
    "back to overview",
    "all news",
    "view all",
)


# The shortest headline that carries meaning. Anchor texts below it are
# navigation ("Vice", "1", "2") rather than the title of an article.
NEWS_TITLE_MINIMUM_CHARACTERS: Final = 12


NEWS_TITLE_MAXIMUM_CHARACTERS: Final = 300


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------


def classify_party_role(
    normalized: str,
) -> tuple[PartyRole, ...]:
    """
    Return the roles a piece of text names, most specific first.

    A statute usually appoints one company to both roles in a single
    sentence, so more than one role can be returned for one label.
    """

    if any(label in normalized for label in COMBINED_PARTY_LABELS):
        return (
            PartyRole.MANAGER,
            PartyRole.ADMINISTRATOR,
        )

    roles: list[PartyRole] = []

    for role, labels in PARTY_ROLE_LABELS:
        if any(label in normalized for label in labels):
            roles.append(role)

    return tuple(roles)


def party_label_offset(
    *,
    normalized: str,
    role: PartyRole,
) -> int | None:
    """Return where the label of a role starts, or None when absent."""

    best: int | None = None

    for candidate_role, labels in PARTY_ROLE_LABELS:
        if candidate_role is not role:
            continue

        for label in labels:
            offset = normalized.find(label)

            if offset >= 0 and (best is None or offset < best):
                best = offset

    return best


def normalize_ico(
    raw_value: str,
) -> str | None:
    """Return an eight digit registration number, or None when invalid."""

    digits = re.sub(
        r"\D",
        "",
        raw_value,
    )

    if len(digits) != 8:
        return None

    return digits


def clean_party_name(
    raw_name: str,
) -> str | None:
    """
    Turn a captured company name into the name alone.

    The capture starts where the label ended, so it can begin with the
    words that joined them, and it can carry a leading bullet or dash of
    a document layout.
    """

    name = " ".join(raw_name.split()).strip(" 	-–—•:,;")

    # A legal name opens with a capital letter or a digit, and it is not
    # introduced by a connective. Both kinds of leading word are dropped
    # in one loop: stripping them separately left "Spolecnosti je CODYA
    # investicni spolecnost" once the tail of a declined label was gone.
    #
    # The date is stripped inside the same loop rather than once before
    # it, because a connective can stand in front of the date: "je
    # pocinaje 10. 05. 2018 AVANT investicni spolecnost, a.s." only
    # exposes its date after "pocinaje" is gone.
    while name:
        without_date = _without_leading_date(name)

        if without_date != name:
            name = without_date

            continue

        head, separator, rest = name.partition(" ")

        folded_head = fold_diacritics(head).strip(".,;:")

        is_noise = folded_head in PARTY_NAME_LEADING_NOISE

        opens_a_name = head[:1].isupper() or head[:1].isdigit()

        if not is_noise and opens_a_name:
            break

        if not separator:
            return None

        name = rest.strip(" 	-–—•:,;")

    if len(name) < 3:
        return None

    if is_generic_company_name(name):
        return None

    folded = fold_diacritics(name)

    if len(folded.split()) > MAXIMUM_COMPANY_NAME_WORDS:
        return None

    words = set(
        re.findall(
            r"[a-z0-9]+",
            folded,
        )
    )

    if any(marker in words for marker in COMPANY_SENTENCE_MARKERS):
        return None

    return name


# A date standing in front of a company name, in the two spellings the
# delivered output holds. A statute writes "s ucinnosti od 4. 10. 2021
# AVANT investicni spolecnost, a.s." and a web footer writes "(c) 2025
# investicni spolecnost", and both dates were kept as part of the name
# because a leading digit was taken to open one.
_LEADING_DATE: Final = re.compile(
    r"""
    ^
    (?:
        \d{1,2}\s*[./]\s*\d{1,2}\s*[./]\s*(?:19|20)\d{2}
        |
        # the same date written with the month as a word, as a statute
        # dates its own effect: "29. ledna 2021 AVANT investicni ..."
        \d{1,2}\s*\.\s*
        (?:ledna|unora|února|brezna|března|dubna|kvetna|května|cervna|června
          |cervence|července|srpna|zari|září|rijna|října|listopadu|prosince)
        \s*(?:19|20)\d{2}
        |
        (?:19|20)\d{2}
    )
    \s*[.,]?\s+
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _without_leading_date(
    name: str,
) -> str:
    """
    Drop a date in front of a company name, when it is safely separable.

    Only a complete date or a bare year is removed, and only when a word
    that can open a company name follows it. "4. 10. 2021 AVANT
    investicni spolecnost, a.s." loses its date; "4stavebni a.s." and
    "3M FUND MSI SICAV a.s." keep every digit they have, because neither
    opens with a date.
    """

    match = _LEADING_DATE.match(name)

    if match is None:
        return name

    rest = name[match.end() :].strip(" 	-–—•:,;")

    if len(rest) < 3:
        return name

    if not (rest[:1].isupper() or rest[:1].islower()):
        return name

    return rest


def is_generic_company_name(
    name: str,
) -> bool:
    """Return whether a captured name is only a legal form."""

    tokens = re.findall(
        r"[a-z0-9]+",
        fold_diacritics(name),
    )

    return not any(token not in GENERIC_COMPANY_TOKENS for token in tokens)


def fold_diacritics(
    value: str,
) -> str:
    """Return lower-case text without diacritics, for label comparison."""

    decomposed = unicodedata.normalize(
        "NFKD",
        value,
    )

    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()


def classify_capital_metric(
    normalized: str,
) -> AumMetricType | None:
    """
    Return the capital figure a text names, judged by the label nearest the value.

    A window regularly carries two labels: the section heading above the figure
    and the wording of the line the figure stands on. The standard annual report
    prints "a) Základní kapitál Fondu" over "Výše fondového kapitálu: 221 913
    tis. Kč", and reading the heading filed a genuine fund-capital figure as
    registered capital, which may never stand in for assets.

    The label that governs a figure is the one closest in front of it, so the
    last occurrence wins. Where two labels start at the same place the longer
    and then the stricter one wins, which keeps the ambiguous case resolving
    towards a refusal rather than towards a value.
    """

    best: tuple[int, int, int, AumMetricType] | None = None

    for order, (metric, labels) in enumerate(CAPITAL_METRIC_LABELS):
        for label in labels:
            position = normalized.rfind(label)

            if position < 0:
                continue

            candidate = (position, len(label), -order, metric)

            if best is None or candidate[:3] > best[:3]:
                best = candidate

    return best[3] if best is not None else None


def describes_manager_level_capital(
    normalized: str,
) -> bool:
    """Return whether a capital figure belongs to the manager or group."""

    return any(marker in normalized for marker in MANAGER_LEVEL_CAPITAL_MARKERS)


def describes_statutory_capital(
    normalized: str,
) -> bool:
    """Return whether a figure is the statutory floor rather than a holding."""

    return any(marker in normalized for marker in STATUTORY_CAPITAL_MARKERS)


def classify_return_series(
    normalized: str,
) -> ReturnSeriesType | None:
    """Return which period a reported performance covers."""

    for series_type, labels in RETURN_SERIES_LABELS:
        if any(label in normalized for label in labels):
            return series_type

    return None


def classify_historical_value(
    normalized: str,
) -> HistoricalValueType | None:
    """Return which quantity a dated value measures."""

    for value_type, labels in HISTORICAL_VALUE_LABELS:
        if any(label in normalized for label in labels):
            return value_type

    return None


def classify_return_type(
    normalized: str,
) -> ReturnType | None:
    """Return which return concept a stated percentage describes."""

    for return_type, labels in RETURN_TYPE_LABELS:
        if any(label in normalized for label in labels):
            return return_type

    return None


def states_no_published_return(
    normalized: str,
) -> bool:
    """Return whether the source says no target return is published."""

    return any(marker in normalized for marker in NOT_PUBLISHED_MARKERS)


def classify_annualization(
    normalized: str,
) -> Annualization:
    """Return whether a rate is per annum, cumulative or period based."""

    if any(marker in normalized for marker in PER_ANNUM_MARKERS):
        return Annualization.PER_ANNUM

    if any(marker in normalized for marker in CUMULATIVE_MARKERS):
        return Annualization.CUMULATIVE

    if any(marker in normalized for marker in PERIOD_MARKERS):
        return Annualization.PERIOD

    return Annualization.UNKNOWN


def declared_frequency(
    normalized: str,
) -> SeriesFrequency | None:
    """Return the frequency a text states in words."""

    for frequency, labels in SERIES_FREQUENCY_LABELS:
        if any(label in normalized for label in labels):
            return frequency

    return None


def frequency_from_gap_days(
    gap_days: float,
) -> SeriesFrequency:
    """Return the frequency implied by the typical gap of a series."""

    for maximum_gap, frequency in FREQUENCY_MAXIMUM_GAP_DAYS:
        if gap_days <= maximum_gap:
            return frequency

    return SeriesFrequency.IRREGULAR


def share_class_code(
    raw_text: str,
) -> str | None:
    """
    Return the share class a text names, or None when it names none.

    The code is read from the original text, because a class is written
    in capitals and normalization would lose that signal.
    """

    match = SHARE_CLASS_PATTERN.search(raw_text)

    if match is not None:
        code = match.group("code").upper()

        if code.isalnum():
            return code

    # A row naming several classes ("Prioritni investicni akcie PIA CZK"
    # and "Vykonnostni investicni akcie VIA CZK" on one line) is decided
    # by the class standing first, which is the one the sentence is
    # about. Iterating the set itself returned whichever member the hash
    # order put first, so the same document produced a different class
    # from one run to the next.
    found = [
        (
            position.start(),
            code,
        )
        for code in sorted(BARE_SHARE_CLASS_CODES)
        for position in [
            re.search(
                rf"\b{code}\b",
                raw_text,
            )
        ]
        if position is not None
    ]

    if not found:
        return None

    return min(found)[1]


def months_from_period(
    *,
    count: int,
    unit: str,
) -> int:
    """Return a holding period in months, whatever unit it was written in."""

    normalized_unit = unit.strip().lower()

    if normalized_unit in YEAR_UNITS:
        return count * MONTHS_PER_YEAR

    return count


def classify_fee_tier_basis(
    normalized: str,
) -> FeeTierBasis | None:
    """Return what decides which tier of a fee applies."""

    if HOLDING_PERIOD_RANGE_PATTERN.search(normalized) or HOLDING_PERIOD_TO_PATTERN.search(
        normalized
    ):
        return FeeTierBasis.HOLDING_PERIOD

    if HOLDING_PERIOD_FROM_PATTERN.search(normalized):
        return FeeTierBasis.HOLDING_PERIOD

    if any(marker in normalized for marker in DISTRIBUTOR_MARKERS):
        return FeeTierBasis.DISTRIBUTOR

    if any(
        marker in normalized
        for marker in (
            "trida",
            "tride",
            "tridy",
            "trid ",
            "class",
        )
    ):
        return FeeTierBasis.SHARE_CLASS

    return None


def is_negotiated_fee(
    normalized: str,
) -> bool:
    """Return whether a fee is agreed individually rather than published."""

    return any(marker in normalized for marker in NEGOTIATED_FEE_MARKERS)


def is_news_path(
    path: str,
) -> bool:
    """Return whether a URL path belongs to a news section."""

    segments = [segment for segment in path.lower().split("/") if segment]

    return any(
        segment == news_segment or segment.startswith(f"{news_segment}-")
        for segment in segments
        for news_segment in NEWS_PATH_SEGMENTS
    )


def is_news_article_path(
    path: str,
) -> bool:
    """
    Return whether a URL points at one article rather than at a listing.

    A listing ends with the section itself; an article carries a slug
    below it.
    """

    segments = [segment for segment in path.lower().split("/") if segment]

    if not segments:
        return False

    for index, segment in enumerate(segments):
        if segment in NEWS_PATH_SEGMENTS and index + 1 < len(segments):
            return True

    return False


def is_news_link_noise(
    normalized_text: str,
) -> bool:
    """Return whether an anchor text is navigation rather than a headline."""

    if len(normalized_text) < NEWS_TITLE_MINIMUM_CHARACTERS:
        return True

    return any(noise == normalized_text for noise in NEWS_LINK_NOISE)
