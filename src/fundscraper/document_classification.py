"""
Classification of a downloaded document into its official type.

A fund publishes the same kinds of document under many names, in two
languages, and often behind an address that says nothing at all. The type
therefore has to be read from every signal available at once: the file
name, the address, the title of the page it was linked from, the anchor
text of that link, the title inside the document, the first page, the
embedded metadata and the vocabulary that repeats through the body.

No single signal decides. Each contributes evidence, and the type with
the strongest combined evidence wins, which is what keeps a statute
called "220701_statut_podfond-1.pdf" apart from a statute called
"dokument.pdf" whose first page says "STATUT PODFONDU".

The module is pure and performs no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import unquote, urlsplit

from fundscraper.html_discovery import normalize_search_text
from fundscraper.output_models import DocumentType


@dataclass(frozen=True, slots=True)
class ClassificationSignals:
    """Everything known about a document when its type is decided."""

    url: str = ""
    anchor_text: str = ""
    page_title: str = ""
    document_title: str = ""
    header_text: str = ""
    metadata_title: str = ""
    metadata_subject: str = ""
    body_text: str = ""


@dataclass(frozen=True, slots=True)
class DocumentClassification:
    """The decided type and the evidence behind it."""

    document_type: DocumentType
    score: int
    matched_keywords: tuple[str, ...] = ()
    signals_used: tuple[str, ...] = field(default_factory=tuple)
    runner_up: DocumentType | None = None
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class TypeRule:
    """The wording that names one official document type."""

    document_type: DocumentType

    # Wording that identifies the type on its own.
    strong: tuple[str, ...] = ()

    # Wording that supports the type but also occurs elsewhere.
    weak: tuple[str, ...] = ()

    # Wording that rules the type out even when its own words appear.
    excluded: tuple[str, ...] = ()

    # Wording that only counts when its companion appears in the same
    # signal. "Podfond" names a subfund on almost every document of a
    # SICAV, so on its own it cannot mean the document is a statute.
    paired: tuple[tuple[str, str], ...] = ()


# Ordered from the most specific type to the least. A subfund statute
# names both "statut" and "podfond", so its rule has to be considered
# before the plain statute claims the document.
TYPE_RULES: Final[tuple[TypeRule, ...]] = (
    TypeRule(
        document_type=DocumentType.PRIIPS_KID,
        strong=(
            "sdeleni klicovych informaci",
            "key information document",
            "klicove informace pro investory",
            "priips",
            "kiid",
        ),
        weak=(
            "klicove informace",
            "klicovych informaci",
            "kid",
            "zamysleny retailovy investor",
            "ukazatel rizika",
            "summary risk indicator",
            "co je tento produkt",
        ),
    ),
    TypeRule(
        document_type=DocumentType.SUBFUND_STATUTE,
        strong=(
            "statut podfondu",
            "subfund statute",
            "sub-fund statute",
        ),
        paired=(
            (
                "podfond",
                "statut",
            ),
        ),
    ),
    TypeRule(
        document_type=DocumentType.STATUTE,
        strong=(
            "statut fondu",
            "statut investicniho fondu",
            "fund statute",
            "uplne zneni statutu",
        ),
        weak=(
            "statut",
            "statute",
        ),
        excluded=("statut podfondu",),
    ),
    TypeRule(
        document_type=DocumentType.FINANCIAL_STATEMENTS,
        strong=(
            "ucetni zaverka",
            "financial statements",
            "rozvaha",
            "vykaz zisku a ztraty",
        ),
        weak=(
            "priloha ucetni zaverky",
            "balance sheet",
            "profit and loss",
        ),
    ),
    TypeRule(
        document_type=DocumentType.HALF_YEAR_REPORT,
        strong=(
            "pololetni zprava",
            "half year report",
            "half-year report",
            "semi annual report",
            "interim report",
        ),
        weak=("pololetni",),
    ),
    TypeRule(
        document_type=DocumentType.ANNUAL_REPORT,
        strong=(
            "vyrocni zprava",
            "annual report",
            "vyrocni zpravy",
        ),
        weak=(
            "vyrocni",
            "za rok koncici",
            "for the year ended",
        ),
        excluded=("pololetni zprava",),
    ),
    TypeRule(
        document_type=DocumentType.PROSPECTUS,
        strong=(
            "prospekt",
            "prospectus",
        ),
        weak=("base prospectus",),
    ),
    TypeRule(
        document_type=DocumentType.MEMORANDUM,
        strong=(
            "investicni memorandum",
            "information memorandum",
            "offering memorandum",
        ),
        weak=("memorandum",),
    ),
    TypeRule(
        document_type=DocumentType.PRICE_LIST,
        strong=(
            "cenik",
            "ceniku",
            "price list",
            "sazebnik",
            "sazebnik poplatku",
        ),
        weak=("fee schedule",),
    ),
    TypeRule(
        document_type=DocumentType.FACTSHEET,
        strong=(
            "factsheet",
            "fact sheet",
            "informacni list",
            "produktovy list",
        ),
        weak=(
            "prehled fondu",
            "fund profile",
        ),
    ),
    TypeRule(
        document_type=DocumentType.INFOLETTER,
        strong=(
            "infoletter",
            "mesicni zprava",
            "kvartalni zprava",
            "ctvrtletni zprava",
            "monthly report",
            "quarterly report",
        ),
        weak=(
            "newsletter",
            "komentar portfolio manazera",
        ),
    ),
    TypeRule(
        document_type=DocumentType.INVESTOR_NOTICE,
        strong=(
            "oznameni investorum",
            "sdeleni akcionarum",
            "oznameni akcionarum",
            "investor notice",
            "notice to investors",
            "oznameni o zmene",
        ),
        weak=(
            "pozvanka na valnou hromadu",
            "zapis z valne hromady",
        ),
    ),
    TypeRule(
        document_type=DocumentType.REGISTER,
        strong=(
            "vypis z obchodniho rejstriku",
            "seznam investicnich fondu",
        ),
        # "Obchodni rejstrik" is named in passing by nearly every Czech
        # corporate document, so it never identifies one on its own.
        paired=(
            (
                "obchodni rejstrik",
                "vypis",
            ),
        ),
    ),
)


# What each signal is worth. A word inside the document outweighs a word
# in the address, because a file name is chosen by whoever uploaded it
# while the title page is part of the document itself.
SIGNAL_WEIGHTS: Final[dict[str, int]] = {
    "document_title": 5,
    "header_text": 5,
    "metadata_title": 4,
    "metadata_subject": 3,
    "anchor_text": 3,
    "file_name": 3,
    "url_path": 2,
    "page_title": 2,
    "body_text": 1,
}


STRONG_KEYWORD_POINTS: Final = 6

WEAK_KEYWORD_POINTS: Final = 2


# How much of the body is read. The title page and the running heads
# carry the type; the rest only repeats the vocabulary.
HEADER_CHARACTERS: Final = 4_000


# Below this the evidence is too thin to name a type at all.
MINIMUM_SCORE: Final = 6


# When the best two types are this close the document is reported as
# ambiguous, so a reviewer sees the doubt rather than a coin toss.
AMBIGUITY_MARGIN: Final = 4


def readable_url_text(
    url: str,
) -> str:
    """Return the address of a document with separators as spaces."""

    if not url:
        return ""

    parts = urlsplit(url)

    return normalize_search_text(
        re.sub(
            r"[-_/.+]",
            " ",
            unquote(f"{parts.path} {parts.query}"),
        )
    )


def file_name_text(
    url: str,
) -> str:
    """Return the readable file name of a document address."""

    if not url:
        return ""

    stem = PurePosixPath(unquote(urlsplit(url).path)).stem

    return normalize_search_text(
        re.sub(
            r"[-_.+]",
            " ",
            stem,
        )
    )


def classify_document(
    signals: ClassificationSignals,
) -> DocumentClassification:
    """
    Decide the official type of one document from every signal at once.

    Each signal contributes the keywords it contains, weighted by how
    much that signal is worth. The result names the winning type, the
    keywords behind it and whether a second type came close enough that
    the decision should not be trusted on its own.
    """

    texts = _signal_texts(signals)

    scores: dict[DocumentType, int] = {}

    matches: dict[DocumentType, list[str]] = {}

    used: dict[DocumentType, set[str]] = {}

    for rule in TYPE_RULES:
        total = 0

        matched: list[str] = []

        contributing: set[str] = set()

        for signal_name, text in texts.items():
            if not text:
                continue

            weight = SIGNAL_WEIGHTS[signal_name]

            if any(keyword in text for keyword in rule.excluded):
                continue

            for keyword in rule.strong:
                if keyword in text:
                    total += STRONG_KEYWORD_POINTS * weight

                    matched.append(keyword)

                    contributing.add(signal_name)

            for keyword in rule.weak:
                if keyword in text:
                    total += WEAK_KEYWORD_POINTS * weight

                    matched.append(keyword)

                    contributing.add(signal_name)

            for keyword, companion in rule.paired:
                if keyword in text and companion in text:
                    total += WEAK_KEYWORD_POINTS * weight

                    matched.append(f"{keyword}+{companion}")

                    contributing.add(signal_name)

        if total > 0:
            scores[rule.document_type] = total

            matches[rule.document_type] = matched

            used[rule.document_type] = contributing

    if not scores:
        return DocumentClassification(
            document_type=DocumentType.OTHER,
            score=0,
        )

    ranked = sorted(
        scores.items(),
        key=lambda item: (
            -item[1],
            _rule_order(item[0]),
        ),
    )

    best_type, best_score = ranked[0]

    if best_score < MINIMUM_SCORE:
        return DocumentClassification(
            document_type=DocumentType.OTHER,
            score=best_score,
        )

    runner_up = ranked[1][0] if len(ranked) > 1 else None

    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    return DocumentClassification(
        document_type=best_type,
        score=best_score,
        matched_keywords=tuple(
            dict.fromkeys(
                matches.get(
                    best_type,
                    [],
                )
            )
        ),
        signals_used=tuple(
            sorted(
                used.get(
                    best_type,
                    set(),
                )
            )
        ),
        runner_up=runner_up,
        ambiguous=(
            runner_up is not None
            and not _is_refinement(
                best_type,
                runner_up,
            )
            and best_score - runner_up_score < AMBIGUITY_MARGIN
        ),
    )


def _signal_texts(
    signals: ClassificationSignals,
) -> dict[str, str]:
    return {
        "document_title": normalize_search_text(signals.document_title),
        "header_text": normalize_search_text(signals.header_text[:HEADER_CHARACTERS]),
        "metadata_title": normalize_search_text(signals.metadata_title),
        "metadata_subject": normalize_search_text(signals.metadata_subject),
        "anchor_text": normalize_search_text(signals.anchor_text),
        "file_name": file_name_text(signals.url),
        "url_path": readable_url_text(signals.url),
        "page_title": normalize_search_text(signals.page_title),
        "body_text": normalize_search_text(signals.body_text[:HEADER_CHARACTERS]),
    }


# Pairs where the second type is a broader form of the first. A subfund
# statute scoring close to a statute is not an ambiguity, it is a
# refinement, and reporting it as doubtful would be misleading.
_REFINEMENTS: Final[frozenset[tuple[DocumentType, DocumentType]]] = frozenset(
    {
        (
            DocumentType.SUBFUND_STATUTE,
            DocumentType.STATUTE,
        ),
        (
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
        ),
        (
            DocumentType.HALF_YEAR_REPORT,
            DocumentType.ANNUAL_REPORT,
        ),
        (
            DocumentType.FINANCIAL_STATEMENTS,
            DocumentType.ANNUAL_REPORT,
        ),
    }
)


def _is_refinement(
    best: DocumentType,
    runner_up: DocumentType,
) -> bool:
    return (
        best,
        runner_up,
    ) in _REFINEMENTS


def _rule_order(
    document_type: DocumentType,
) -> int:
    for index, rule in enumerate(TYPE_RULES):
        if rule.document_type is document_type:
            return index

    return len(TYPE_RULES)
