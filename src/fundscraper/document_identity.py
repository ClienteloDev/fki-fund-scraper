"""
Which fund a downloaded document actually belongs to.

A manager hosts the documents of every fund it runs on one domain, in one
folder, often with file names that name no fund at all. Deciding what a
document describes therefore has to happen before any value is read out
of it, and it has to happen from the document itself rather than from
where it was found.

The decision weighs several independent kinds of evidence: the legal name
of the fund, its registration number, the ISIN of a share class, the name
of a subfund, the title page, the headers and footers that repeat on
every page, the embedded metadata, the address, and the scope discovery
already assigned in step 5.

Two rules govern the outcome. Being hosted on the domain of the manager
never makes a document belong to a fund. And evidence that contradicts
itself produces an ambiguous result, never a silent acceptance.

The module is pure and performs no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from fundscraper.discovery_priority import distinctive_tokens
from fundscraper.document_classification import file_name_text, readable_url_text
from fundscraper.html_discovery import normalize_search_text


class DocumentScope(StrEnum):
    """Which entity a document describes."""

    EXACT_FUND = "exact_fund"
    SUBFUND = "subfund"
    SHARE_CLASS = "share_class"
    MANAGER = "manager"
    ANOTHER_FUND = "another_fund"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"


# Scopes whose documents may be read for this fund.
ACCEPTED_SCOPES: Final[frozenset[DocumentScope]] = frozenset(
    {
        DocumentScope.EXACT_FUND,
        DocumentScope.SUBFUND,
        DocumentScope.SHARE_CLASS,
    }
)


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """One fact that supports or contradicts an attribution."""

    kind: str
    detail: str
    supports_fund: bool


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    """The entity a document was attributed to, and why."""

    scope: DocumentScope
    confidence: str
    evidence: tuple[IdentityEvidence, ...] = ()
    subfund_name: str | None = None
    share_class: str | None = None
    matched_ico: str | None = None
    matched_isin: str | None = None
    rejection_reason: str | None = None

    @property
    def is_accepted(self) -> bool:
        return self.scope in ACCEPTED_SCOPES


@dataclass(frozen=True, slots=True)
class IdentityInput:
    """Everything the decision is allowed to look at."""

    fund_name: str
    fund_ico: str | None = None
    url: str = ""
    title_text: str = ""
    repeated_text: str = ""
    metadata_text: str = ""
    body_text: str = ""
    discovery_scope: str | None = None


ICO_PATTERN: Final = re.compile(
    r"""
    (?:i[čc]o?|identifika[čc]n[íi]\s+[čc][íi]slo)
    \s*:?\s*
    (?P<ico>\d[\d\s]{6,12}\d)
    """,
    re.IGNORECASE | re.VERBOSE,
)


ISIN_PATTERN: Final = re.compile(r"\b(?P<isin>[A-Z]{2}[A-Z0-9]{9}\d)\b")


SUBFUND_PATTERN: Final = re.compile(
    r"""
    # The label ignores case, because a title page shouts "STATUT
    # PODFONDU". The name after it must still open with a capital.
    (?i:podfond(?:u|em)?)
    \s+
    (?P<name>
        # The Unicode range A-Z plus accents would also cover the
        # lower-case letters that sit between them, which let the
        # sentence "podfondu cinnosti dle statutu" pass as a name.
        [A-Z0-9ÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]
        [^\n,.;()]{1,60}?
    )
    (?=[\n,.;()]|$)
    """,
    re.VERBOSE,
)


SHARE_CLASS_PATTERN: Final = re.compile(
    r"""
    (?i:t[rř][íi]d[aeěuy]?(?:\s+investi[čc]n[íi]ch\s+akci[íi])?|class)
    [\s:]+
    (?P<code>[A-Z]{1,4}\d?)
    \b
    """,
    re.VERBOSE,
)


# Wording proving a document describes the manager rather than a fund.
MANAGER_MARKERS: Final[tuple[str, ...]] = (
    "vyrocni zprava investicni spolecnosti",
    "vyrocni zprava spolecnosti",
    "obhospodarovatel spravuje",
    "investicni spolecnost spravuje",
    "aktiva ve sprave skupiny",
    "seznam obhospodarovanych fondu",
    "prehled fondu ve sprave",
    "annual report of the management company",
)


# How far into the body the identity is looked for when the title page
# proves nothing. The name of a fund repeats in the running head of every
# page, so the whole document is worth reading rather than its opening.
BODY_CHARACTERS: Final = 40_000


def resolve_document_identity(
    identity_input: IdentityInput,
) -> DocumentIdentity:
    """
    Decide which entity a document belongs to.

    Evidence is gathered from every source separately and only then
    weighed, so a contradiction between the title page and the address
    is visible rather than resolved by whichever was checked first.
    """

    fund_tokens = distinctive_tokens(identity_input.fund_name)

    texts = _identity_texts(identity_input)

    evidence: list[IdentityEvidence] = []

    matched_ico = _matching_ico(
        texts=texts,
        fund_ico=identity_input.fund_ico,
    )

    if matched_ico is not None:
        evidence.append(
            IdentityEvidence(
                kind="ico",
                detail=matched_ico,
                supports_fund=True,
            )
        )

    foreign_ico = _foreign_ico(
        texts=texts,
        fund_ico=identity_input.fund_ico,
    )

    naming = _name_evidence(
        texts=texts,
        fund_tokens=fund_tokens,
    )

    evidence.extend(naming)

    manager_marker = _first_marker(
        texts=texts,
        markers=MANAGER_MARKERS,
    )

    if manager_marker is not None:
        evidence.append(
            IdentityEvidence(
                kind="manager_wording",
                detail=manager_marker,
                supports_fund=False,
            )
        )

    supporting = [item for item in evidence if item.supports_fund]

    contradicting = [item for item in evidence if not item.supports_fund]

    if not fund_tokens and matched_ico is None:
        return DocumentIdentity(
            scope=DocumentScope.UNKNOWN,
            confidence="low",
            evidence=tuple(evidence),
            rejection_reason=("The fund name carries no distinctive word to match on."),
        )

    if supporting and contradicting:
        # The document names this fund and something that is not it. A
        # value taken from it could belong to either.
        return DocumentIdentity(
            scope=DocumentScope.AMBIGUOUS,
            confidence="low",
            evidence=tuple(evidence),
            rejection_reason=(
                "The document carries evidence for this fund and against "
                f"it at the same time: {contradicting[0].kind}"
            ),
        )

    if contradicting:
        scope = (
            DocumentScope.MANAGER
            if any(item.kind == "manager_wording" for item in contradicting)
            else DocumentScope.ANOTHER_FUND
        )

        return DocumentIdentity(
            scope=scope,
            confidence="medium",
            evidence=tuple(evidence),
            rejection_reason=(f"The document describes {scope.value}: {contradicting[0].detail}"),
        )

    if not supporting:
        if foreign_ico is not None:
            return DocumentIdentity(
                scope=DocumentScope.ANOTHER_FUND,
                confidence="medium",
                evidence=(
                    IdentityEvidence(
                        kind="foreign_ico",
                        detail=foreign_ico,
                        supports_fund=False,
                    ),
                ),
                rejection_reason=(
                    f"The document states registration number {foreign_ico}, "
                    "which is not the one of this fund."
                ),
            )

        return DocumentIdentity(
            scope=DocumentScope.UNKNOWN,
            confidence="low",
            evidence=tuple(evidence),
            rejection_reason=("No evidence in the document ties it to this fund."),
        )

    subfund = _subfund_name(identity_input)

    share_class = _share_class(identity_input)

    isin = _first_isin(texts)

    scope = DocumentScope.EXACT_FUND

    if share_class is not None:
        scope = DocumentScope.SHARE_CLASS
    elif subfund is not None:
        scope = DocumentScope.SUBFUND

    return DocumentIdentity(
        scope=scope,
        confidence=_confidence_of(
            supporting=supporting,
            matched_ico=matched_ico,
        ),
        evidence=tuple(evidence),
        subfund_name=subfund,
        share_class=share_class,
        matched_ico=matched_ico,
        matched_isin=isin,
    )


def _identity_texts(
    identity_input: IdentityInput,
) -> dict[str, str]:
    """Return every text the identity may be read from, normalized."""

    return {
        "title": normalize_search_text(identity_input.title_text),
        "repeated": normalize_search_text(identity_input.repeated_text),
        "metadata": normalize_search_text(identity_input.metadata_text),
        "file_name": file_name_text(identity_input.url),
        "url": readable_url_text(identity_input.url),
        "body": normalize_search_text(identity_input.body_text[:BODY_CHARACTERS]),
    }


def _name_evidence(
    *,
    texts: dict[str, str],
    fund_tokens: tuple[str, ...],
) -> list[IdentityEvidence]:
    """Return where the name of the fund was and was not found."""

    if not fund_tokens:
        return []

    evidence: list[IdentityEvidence] = []

    for name, text in texts.items():
        if not text:
            continue

        if all(token in text for token in fund_tokens):
            evidence.append(
                IdentityEvidence(
                    kind=f"name_in_{name}",
                    detail=" ".join(fund_tokens),
                    supports_fund=True,
                )
            )

    return evidence


def _matching_ico(
    *,
    texts: dict[str, str],
    fund_ico: str | None,
) -> str | None:
    if not fund_ico:
        return None

    for text in texts.values():
        for match in ICO_PATTERN.finditer(text):
            digits = re.sub(
                r"\D",
                "",
                match.group("ico"),
            )

            if digits == fund_ico:
                return digits

    return None


def _foreign_ico(
    *,
    texts: dict[str, str],
    fund_ico: str | None,
) -> str | None:
    """Return a registration number that is not the one of this fund."""

    if not fund_ico:
        return None

    for text in texts.values():
        for match in ICO_PATTERN.finditer(text):
            digits = re.sub(
                r"\D",
                "",
                match.group("ico"),
            )

            if len(digits) == 8 and digits != fund_ico:
                return digits

    return None


def _first_isin(
    texts: dict[str, str],
) -> str | None:
    for name in (
        "title",
        "repeated",
        "metadata",
        "body",
    ):
        match = ISIN_PATTERN.search(texts.get(name, "").upper())

        if match is not None:
            return match.group("isin")

    return None


def _subfund_name(
    identity_input: IdentityInput,
) -> str | None:
    for raw_text in (
        identity_input.title_text,
        identity_input.repeated_text,
        identity_input.body_text[:BODY_CHARACTERS],
    ):
        for match in SUBFUND_PATTERN.finditer(raw_text):
            name = " ".join(match.group("name").split())

            if _looks_like_a_name(name):
                return name

    return None


# The longest name a subfund carries. Anything longer is a sentence that
# happened to start after the word "podfond".
MAXIMUM_SUBFUND_NAME: Final = 50


# Shorter than this is a truncated word, not a name.
MINIMUM_SUBFUND_NAME: Final = 3


def _looks_like_a_name(
    name: str,
) -> bool:
    """
    Return whether a capture after "podfond" is a name and not a sentence.

    "Podfond ESG SeniorCARE" names a subfund. "Podfond vydava investicni
    akcie" continues a sentence, and storing it produced a subfund called
    "Fond vydava investicni akcie".
    """

    if not MINIMUM_SUBFUND_NAME <= len(name) <= MAXIMUM_SUBFUND_NAME:
        return False

    if ":" in name:
        # A label such as "Nazev:" introduces the name; it is not one.
        return False

    folded = fold_diacritics(name)

    if any(word in folded for word in SHARE_CLASS_WORDS):
        # "Akcie tridy PIA CZK" names a share class of a subfund, not the
        # subfund itself, and storing it confuses the two levels.
        return False

    tokens = name.split()

    # A trailing one letter word is the start of the next clause, as in
    # "Max Realitni Fond SICAV a".
    if len(tokens[-1].strip(".,")) < 2:
        return False

    # A subfund name is the distinctive part, not the legal wording every
    # fund shares. Two or more of those words mean the capture ran into
    # the name of the fund itself rather than of its subfund.
    generic = sum(1 for token in tokens if fold_diacritics(token.strip(".,")) in GENERIC_FUND_WORDS)

    if generic >= 2:
        return False

    if len(tokens) == 1:
        return True

    # A name carries more than one capitalised word; a sentence carries
    # one capital at its start and lower case after it.
    return any(token[:1].isupper() for token in tokens[1:])


# Legal wording shared by every Czech fund. It never distinguishes one
# subfund from another.
SHARE_CLASS_WORDS: Final[tuple[str, ...]] = (
    "akcie",
    "akcii",
    "trida",
    "tridy",
    "tride",
    "class",
)


GENERIC_FUND_WORDS: Final[frozenset[str]] = frozenset(
    {
        "fond",
        "fondu",
        "fondy",
        "fund",
        "funds",
        "sicav",
        "investicni",
        "investment",
        "spolecnost",
        "podfond",
        "podfondu",
        "subfund",
        "as",
    }
)


def fold_diacritics(
    value: str,
) -> str:
    """Return lower-case text without diacritics."""

    decomposed = unicodedata.normalize(
        "NFKD",
        value,
    )

    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()


def _share_class(
    identity_input: IdentityInput,
) -> str | None:
    for raw_text in (
        identity_input.title_text,
        identity_input.repeated_text,
    ):
        match = SHARE_CLASS_PATTERN.search(raw_text)

        if match is not None:
            return match.group("code").upper()

    return None


def _first_marker(
    *,
    texts: dict[str, str],
    markers: tuple[str, ...],
) -> str | None:
    for text in texts.values():
        for marker in markers:
            if marker in text:
                return marker

    return None


def _confidence_of(
    *,
    supporting: list[IdentityEvidence],
    matched_ico: str | None,
) -> str:
    if matched_ico is not None:
        return "high"

    strong_places = {"name_in_title", "name_in_repeated", "name_in_metadata"}

    if any(item.kind in strong_places for item in supporting):
        return "high"

    if len(supporting) > 1:
        return "medium"

    return "low"
