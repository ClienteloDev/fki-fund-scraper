"""
Priority of a page or a document during official-source discovery.

Discovery has to spend a limited crawl budget on the pages that actually
carry fund documents. This module decides what is worth fetching next and
how strongly a link looks like an official fund document, from signals
that are available before anything is downloaded: the URL path, the
anchor text, the title of the page it was found on, the file name and the
name of the fund itself.

The module is pure. It performs no I/O and holds no crawl state, so the
same scoring is used by the crawler, by the sitemap filter and by the
tests.

All matching runs on text passed through
:func:`fundscraper.html_discovery.normalize_search_text`, so every literal
below is written in lower case without diacritics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import unquote, urlsplit

from fundscraper.html_discovery import normalize_search_text
from fundscraper.output_models import DocumentType

# The order in which official documents are wanted. A key information
# document answers more of the delivered fields than an annual report, so
# a crawl that can only reach a few documents should reach these first.
DOCUMENT_TYPE_PRIORITY: Final[
    tuple[
        tuple[
            DocumentType,
            int,
        ],
        ...,
    ]
] = (
    (DocumentType.PRIIPS_KID, 100),
    (DocumentType.SUBFUND_STATUTE, 95),
    (DocumentType.STATUTE, 90),
    (DocumentType.FACTSHEET, 80),
    (DocumentType.ANNUAL_REPORT, 70),
    (DocumentType.FINANCIAL_STATEMENTS, 65),
    (DocumentType.HALF_YEAR_REPORT, 60),
    (DocumentType.MEMORANDUM, 55),
    (DocumentType.INFOLETTER, 45),
    (DocumentType.REGISTER, 30),
    (DocumentType.MARKETING_PAGE, 20),
    (DocumentType.OTHER, 10),
)


DOCUMENT_TYPE_RANK: Final[dict[DocumentType, int]] = dict(DOCUMENT_TYPE_PRIORITY)


# Wording that names an official fund document outright. Both language
# variants are needed: a Czech fund publishes its statute as "statut" and
# its key information document under the English abbreviation.
DOCUMENT_KEYWORDS: Final[tuple[tuple[str, int], ...]] = (
    ("priips", 60),
    ("sdeleni klicovych informaci", 60),
    ("klicove informace", 55),
    ("kiid", 55),
    ("kid", 45),
    ("statut", 55),
    ("statute", 55),
    ("factsheet", 50),
    ("fact sheet", 50),
    ("informacni list", 45),
    ("produktovy list", 45),
    ("vyrocni zprava", 50),
    ("vyrocni zpravy", 50),
    ("annual report", 50),
    ("ucetni zaverka", 45),
    ("financial statements", 45),
    ("pololetni zprava", 40),
    ("half year report", 40),
    ("memorandum", 40),
    ("prospekt", 40),
    ("prospectus", 40),
    ("mesicni zprava", 35),
    ("kvartalni zprava", 35),
    ("monthly report", 35),
    ("quarterly report", 35),
)


# Sections of a fund website that lead to the documents. Reaching them is
# worth as much as reaching a document directly, because everything below
# them is official material of the fund.
SECTION_KEYWORDS: Final[tuple[tuple[str, int], ...]] = (
    ("povinne informace", 55),
    ("povinne uverejnovane informace", 55),
    ("ke stazeni", 50),
    ("dokumenty", 50),
    ("dokumenty fondu", 55),
    ("documents", 50),
    ("document", 40),
    ("download", 40),
    ("pro investory", 50),
    ("pro-investory", 50),
    ("for investors", 50),
    ("investor", 40),
    ("investory", 40),
    ("informace pro investory", 55),
    ("reporting", 40),
    ("zpravy", 30),
    ("archiv", 35),
    ("archive", 35),
    ("o fondu", 40),
    ("about the fund", 40),
    ("nase fondy", 45),
    ("our funds", 45),
    ("fondy", 35),
    ("funds", 35),
)


# Wording that names a fund or one of its parts. It raises a page above
# the ordinary corporate content of the same domain.
FUND_KEYWORDS: Final[tuple[tuple[str, int], ...]] = (
    ("podfond", 45),
    ("subfund", 45),
    ("sub-fund", 45),
    ("fond", 25),
    ("fund", 25),
    ("sicav", 30),
    ("trida akcii", 35),
    ("share class", 35),
)


# Corporate pages that never hold fund documents. A deeper crawl reaches
# many more of them than a shallow one, so they are refused explicitly.
UNRELATED_KEYWORDS: Final[tuple[str, ...]] = (
    "kariera",
    "kariery",
    "career",
    "volna mista",
    "gdpr",
    "ochrana osobnich udaju",
    "privacy",
    "cookie",
    "cookies",
    "kontakt",
    "contact",
    "obchodni podminky",
    "terms of use",
    "prihlaseni",
    "login",
    "registrace",
    "kosik",
    "fotogalerie",
    "gallery",
    "mapa stranek",
    "sitemap.html",
    "eshop",
    "e-shop",
)


# A path segment that carries a year. An archive of annual reports is
# organised by it, and the most recent one is wanted first.
YEAR_PATTERN: Final = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


# The score at which a link is worth fetching during official discovery.
# Below it a link is ordinary site furniture.
NAVIGATION_THRESHOLD: Final = 30

DOCUMENT_THRESHOLD: Final = 40


class DiscoveryMethod(StrEnum):
    """How one page or document entered the discovery."""

    SEED = "seed"
    NAVIGATION = "navigation"
    SITEMAP = "sitemap"
    DOCUMENT_LINK = "document_link"
    MANAGER_PROFILE = "manager_profile"
    FALLBACK_ADAPTER = "fallback_adapter"


@dataclass(frozen=True, slots=True)
class PrioritySignals:
    """Everything known about a link before it is fetched."""

    url: str
    anchor_text: str = ""
    page_title: str = ""
    fund_name: str = ""
    subfund_name: str = ""
    document_type: DocumentType | None = None


@dataclass(frozen=True, slots=True)
class PriorityScore:
    """The rank of one link and the reasons behind it."""

    value: int
    matched_fund_tokens: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def names_the_fund(self) -> bool:
        return bool(self.matched_fund_tokens)


# Words shared by every Czech fund name. They prove nothing about which
# fund a page belongs to.
GENERIC_NAME_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "as",
        "sicav",
        "fond",
        "fondy",
        "fund",
        "funds",
        "investicni",
        "investment",
        "investments",
        "invest",
        "spolecnost",
        "company",
        "podfond",
        "subfund",
        "otevreny",
        "uzavreny",
        "promennym",
        "zakladnim",
        "kapitalem",
        "cesky",
        "czech",
    }
)


def distinctive_tokens(
    name: str,
) -> tuple[str, ...]:
    """Return the words that tell one fund apart from its siblings."""

    tokens: list[str] = []

    for token in re.findall(
        r"[a-z0-9]+",
        normalize_search_text(name),
    ):
        if len(token) < 3 or token in GENERIC_NAME_TOKENS:
            continue

        if token not in tokens:
            tokens.append(token)

    return tuple(tokens)


def searchable_text(
    signals: PrioritySignals,
) -> str:
    """
    Return the text a link is judged by.

    The URL is unquoted and its separators are turned into spaces, so a
    slug such as "vyrocni-zprava-2024" matches the same keywords as the
    anchor text "Výroční zpráva 2024".
    """

    parts = urlsplit(signals.url)

    path = unquote(f"{parts.path} {parts.query}")

    readable_path = re.sub(
        r"[-_/.+]",
        " ",
        path,
    )

    return normalize_search_text(
        " ".join(
            (
                signals.anchor_text,
                readable_path,
                signals.page_title,
            )
        )
    )


def file_name_of(
    url: str,
) -> str:
    """Return the readable file name of a URL, without its extension."""

    name = PurePosixPath(unquote(urlsplit(url).path)).stem

    return normalize_search_text(
        re.sub(
            r"[-_.+]",
            " ",
            name,
        )
    )


# What a link is worth when it is one of the document types the pass was
# sent out to find. It is large enough to lift a wanted document over an
# unwanted one of the same standing, and small enough not to lift it over
# a link that names the fund.
WANTED_DOCUMENT_TYPE_BONUS: Final = 60


def score_link(
    signals: PrioritySignals,
    *,
    wanted_document_types: frozenset[DocumentType] = frozenset(),
) -> PriorityScore:
    """
    Rank one link for official-source discovery.

    The score adds up independent evidence rather than picking a single
    winner: a document keyword, a section keyword, the name of the fund
    and the type already detected each contribute, because a link that
    carries several of them is the one worth fetching first.

    ``wanted_document_types`` is what makes the deep pass field-aware. A
    fund whose assets are missing is sent out for annual reports and
    financial statements, and those links are then fetched before the
    key information document the fast pass already read.
    """

    text = searchable_text(signals)

    file_name = file_name_of(signals.url)

    value = 0

    reasons: list[str] = []

    document_hit = _best_keyword(
        text=f"{text} {file_name}",
        keywords=DOCUMENT_KEYWORDS,
    )

    if document_hit is not None:
        keyword, points = document_hit

        value += points

        reasons.append(f"document_keyword:{keyword}")

    section_hit = _best_keyword(
        text=text,
        keywords=SECTION_KEYWORDS,
    )

    if section_hit is not None:
        keyword, points = section_hit

        value += points

        reasons.append(f"section_keyword:{keyword}")

    fund_hit = _best_keyword(
        text=text,
        keywords=FUND_KEYWORDS,
    )

    if fund_hit is not None:
        keyword, points = fund_hit

        value += points

        reasons.append(f"fund_keyword:{keyword}")

    matched_tokens = tuple(
        token for token in distinctive_tokens(signals.fund_name) if token in text
    )

    if matched_tokens:
        # Naming the fund is the strongest signal there is on a manager
        # domain that hosts many funds.
        value += min(
            len(matched_tokens) * 35,
            70,
        )

        reasons.append("fund_name:" + ",".join(matched_tokens))

    subfund_tokens = tuple(
        token for token in distinctive_tokens(signals.subfund_name) if token in text
    )

    if subfund_tokens:
        value += 30

        reasons.append("subfund_name:" + ",".join(subfund_tokens))

    if signals.document_type is not None:
        rank = DOCUMENT_TYPE_RANK.get(
            signals.document_type,
            0,
        )

        value += rank // 2

        reasons.append(f"document_type:{signals.document_type.value}")

        if signals.document_type in wanted_document_types:
            value += WANTED_DOCUMENT_TYPE_BONUS

            reasons.append(f"wanted_document_type:{signals.document_type.value}")

    year = _latest_year(text)

    if year is not None:
        # A dated document is preferred over an undated one, and a recent
        # year over an old one. Older versions keep a positive score
        # because they carry the history the extended fields need.
        value += max(
            0,
            min(
                20,
                (year - 2015) * 2,
            ),
        )

        reasons.append(f"year:{year}")

    if any(keyword in text for keyword in UNRELATED_KEYWORDS):
        value -= 80

        reasons.append("unrelated_page")

    return PriorityScore(
        value=max(
            value,
            0,
        ),
        matched_fund_tokens=matched_tokens,
        reasons=tuple(reasons),
    )


def _best_keyword(
    *,
    text: str,
    keywords: tuple[tuple[str, int], ...],
) -> tuple[str, int] | None:
    """Return the highest scoring keyword the text contains."""

    best: tuple[str, int] | None = None

    for keyword, points in keywords:
        if keyword not in text:
            continue

        if best is None or points > best[1]:
            best = (
                keyword,
                points,
            )

    return best


def _latest_year(
    text: str,
) -> int | None:
    years = [int(match.group(0)) for match in YEAR_PATTERN.finditer(text)]

    if not years:
        return None

    return max(years)


def is_unrelated(
    signals: PrioritySignals,
) -> bool:
    """Return whether a link is ordinary corporate content."""

    return any(keyword in searchable_text(signals) for keyword in UNRELATED_KEYWORDS)


def document_type_rank(
    document_type: DocumentType | None,
) -> int:
    """Return how strongly a document type is wanted."""

    if document_type is None:
        return 0

    return DOCUMENT_TYPE_RANK.get(
        document_type,
        0,
    )
