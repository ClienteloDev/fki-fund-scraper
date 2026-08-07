from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from fundscraper.document_parser import (
    DocumentFormat,
    DocumentParseError,
    parse_markup_document,
)
from fundscraper.domain_candidates import (
    PARKED_DOMAIN_FRAGMENTS,
    CandidateSourceName,
    DomainCandidate,
    distinctive_name_tokens,
    generic_name_tokens,
    hostname_preference,
    is_excluded_domain,
    strip_legal_suffixes,
)
from fundscraper.html_discovery import (
    DISCOVERY_RULES,
    HtmlDiscoveryError,
    decode_html_bytes,
    is_html_response,
    normalize_search_text,
    parse_html_page,
)
from fundscraper.http_client import (
    FetchError,
    HttpFetcher,
)
from fundscraper.normalization import canonical_domain, canonical_url
from fundscraper.output_models import DocumentType

# Documents that raise confidence that the page presents a real fund.
# They are a bonus signal and are never required for acceptance.
RELEVANT_DOCUMENT_TYPES: Final[frozenset[DocumentType]] = frozenset(
    {
        DocumentType.PRIIPS_KID,
        DocumentType.STATUTE,
        DocumentType.SUBFUND_STATUTE,
        DocumentType.MEMORANDUM,
        DocumentType.ANNUAL_REPORT,
        DocumentType.HALF_YEAR_REPORT,
        DocumentType.FINANCIAL_STATEMENTS,
        DocumentType.FACTSHEET,
    }
)


# "statut" alone also matches "statutarni organ" in running page text.
# It stays available for link classification, where the anchor text and
# the URL make the meaning unambiguous.
AMBIGUOUS_TEXT_KEYWORDS: Final[frozenset[str]] = frozenset({"statut"})


def _relevant_document_keywords() -> tuple[str, ...]:
    """Reuse discovery keywords of relevant document types for page text."""

    keywords: list[str] = []

    for rule in DISCOVERY_RULES:
        if rule.document_type not in RELEVANT_DOCUMENT_TYPES:
            continue

        for keyword in rule.keywords:
            if keyword in AMBIGUOUS_TEXT_KEYWORDS:
                continue

            if keyword not in keywords:
                keywords.append(keyword)

    return tuple(keywords)


RELEVANT_TEXT_KEYWORDS: Final[tuple[str, ...]] = _relevant_document_keywords()


DOCUMENT_SECTION_KEYWORDS: Final[tuple[str, ...]] = (
    "dokumenty",
    "documents",
    "ke stazeni",
    "pro investory",
    "for investors",
)


# Wording proving the page presents a fund rather than an unrelated
# company that happens to share a distinctive name token.
FUND_CONTEXT_KEYWORDS: Final[tuple[str, ...]] = (
    "sicav",
    "podfond",
    "investicni fond",
    "fond kvalifikovanych investoru",
    "kvalifikovan",
    "investicni spolecnost",
    "obhospodarovatel",
    "administrator fondu",
    "depozitar",
    "investicni strategie",
    "zhodnoceni",
    "investory",
    "investors",
    "investment fund",
    "fund",
    "fondu",
    "fondy",
    "fond ",
)


PARKED_PAGE_MARKERS: Final[tuple[str, ...]] = (
    "this domain is for sale",
    "domain is for sale",
    "domain for sale",
    "buy this domain",
    "domena je na prodej",
    "domena na prodej",
    "tato domena je na prodej",
    "parkovana domena",
    "koupit domenu",
    "the domain name is available",
    "under construction",
    "pripravujeme",
)


# News items, announcements and press releases regularly mention several
# funds at once, so they never prove which fund owns the website.
NEWS_PATH_SEGMENTS: Final[tuple[str, ...]] = (
    "oznameni",
    "aktuality",
    "novinky",
    "tiskove-zpravy",
    "tiskova-zprava",
    "news",
    "press",
    "blog",
    "clanek",
    "clanky",
    "article",
)


def is_news_page_url(
    page_url: str,
) -> bool:
    """Return whether a URL points at a news or announcement item."""

    segments = [segment for segment in urlsplit(page_url).path.casefold().split("/") if segment]

    return any(
        segment == fragment or segment.startswith(f"{fragment}-")
        for segment in segments
        for fragment in NEWS_PATH_SEGMENTS
    )


MAXIMUM_VERIFIED_TEXT_CHARACTERS: Final = 60_000


# A fund named only by generic wording must match its complete legal name,
# and that name must be long enough not to be a common substring.
MINIMUM_GENERIC_NAME_LENGTH: Final = 12


SCORE_NAME_IN_TITLE: Final = 50
SCORE_NAME_IN_HEADING: Final = 35
SCORE_NAME_IN_TEXT: Final = 25
SCORE_EXACT_NAME_BONUS: Final = 15
SCORE_HOSTNAME_TOKEN: Final = 12
SCORE_HOSTNAME_FUND_TOKEN: Final = 18

# A fund whose name carries several distinctive words is far better
# evidence when all of them match, and much weaker when only one does.
SCORE_MULTI_TOKEN_BONUS: Final = 10
SCORE_MULTI_TOKEN_MAXIMUM: Final = 20
PENALTY_PARTIAL_NAME: Final = 15
SCORE_GENERIC_TOKEN_MAXIMUM: Final = 9
SCORE_FUND_CONTEXT: Final = 15
SCORE_DOCUMENT_LINK: Final = 25
SCORE_DOCUMENT_LINK_EXTRA: Final = 5
SCORE_DOCUMENT_LINK_MAXIMUM: Final = 40
SCORE_DOCUMENT_TEXT_REFERENCE: Final = 10
SCORE_DOCUMENT_SECTION: Final = 5


# The threshold is deliberately reachable by a fund page that names the
# fund only in its body text, for example the page of its administrator,
# without linking any document.
DEFAULT_MINIMUM_SCORE: Final = 60
DEFAULT_REVIEW_SCORE: Final = 40
DEFAULT_AMBIGUITY_MARGIN: Final = 15


class MatchLocation(StrEnum):
    """Where a fund name match was found on a candidate page."""

    TITLE = "title"
    HEADING = "heading"
    URL_PATH = "url_path"
    CONTENT = "content"
    MANAGER_INFO = "manager_info"
    DOCUMENT = "document"


# How much each location contributes. The strongest location wins and the
# remaining ones only add a small confirmation bonus.
LOCATION_SCORES: Final[dict[MatchLocation, int]] = {
    MatchLocation.TITLE: 50,
    MatchLocation.HEADING: 40,
    MatchLocation.URL_PATH: 30,
    MatchLocation.CONTENT: 25,
    MatchLocation.MANAGER_INFO: 20,
    MatchLocation.DOCUMENT: 20,
}


SCORE_ADDITIONAL_LOCATION: Final = 8
SCORE_ADDITIONAL_LOCATION_MAXIMUM: Final = 24


# Body text alone is the weakest possible evidence: any page listing many
# funds contains it. An ordinary search result must therefore also match
# in a structural location, or expose a fund document. A fund profile
# returned by a known manager adapter is already an authoritative source
# and is exempt from this requirement.
STRUCTURAL_MATCH_LOCATIONS: Final[frozenset[MatchLocation]] = frozenset(
    {
        MatchLocation.TITLE,
        MatchLocation.HEADING,
        MatchLocation.URL_PATH,
        MatchLocation.MANAGER_INFO,
        MatchLocation.DOCUMENT,
    }
)


@dataclass(frozen=True, slots=True)
class PageContent:
    """The parts of a main page used for verification."""

    title: str | None
    headings: str
    site_name: str
    visible_text: str
    manager_info: str = ""
    url_path: str = ""
    document_text: str = ""


@dataclass(frozen=True, slots=True)
class NameSignals:
    """
    How strongly a page identifies the requested fund.

    A strong match requires several words of the fund name, either as a
    contiguous phrase such as "ambeat invest", or as every distinctive
    word of a multi-word name. One shared word is never sufficient.
    """

    distinctive_tokens: tuple[str, ...]
    generic_tokens: tuple[str, ...]
    distinctive_coverage: float
    generic_coverage: float
    strong_locations: tuple[MatchLocation, ...]
    matched_phrase: str | None
    exact_name_match: bool
    hostname_token_match: bool

    @property
    def identified(self) -> bool:
        """Whether a strong multi-word fund name match was found."""

        return bool(self.strong_locations)

    @property
    def in_title(self) -> bool:
        return MatchLocation.TITLE in self.strong_locations

    @property
    def in_heading(self) -> bool:
        return MatchLocation.HEADING in self.strong_locations

    @property
    def in_text(self) -> bool:
        return MatchLocation.CONTENT in self.strong_locations


@dataclass(frozen=True, slots=True)
class DocumentSignals:
    """Which fund documents the main page links to or mentions."""

    document_types: tuple[str, ...]
    document_urls: tuple[str, ...]
    text_references: tuple[str, ...]
    has_document_section: bool

    @property
    def present(self) -> bool:
        return bool(self.document_types) or bool(self.text_references)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of the short main-page verification of one candidate."""

    candidate: DomainCandidate
    page_url: str | None
    accepted: bool
    score: int
    reason: str
    name_signals: NameSignals | None = None
    document_signals: DocumentSignals | None = None
    fund_context: bool = False
    parked: bool = False
    error: str | None = None
    evidence: tuple[str, ...] = ()


async def verify_candidate(
    *,
    fund_name: str,
    candidate: DomainCandidate,
    fetcher: HttpFetcher,
    minimum_score: int = DEFAULT_MINIMUM_SCORE,
    force: bool = False,
) -> VerificationResult:
    """
    Download only the main page of a candidate domain and verify it.

    A candidate is accepted when the page clearly identifies the exact
    fund. Fund documents raise confidence but are not required.
    """

    # The exact search or fund profile URL is checked first. It names the
    # fund far more precisely than the homepage of a manager site. The
    # domain main page stays as the fallback.
    fetch_urls: list[str] = []

    # A news item or announcement mentions several funds at once, so it
    # is never usable evidence. Only the fund page itself is checked.
    result_url = (
        None
        if candidate.result_url is None or is_news_page_url(candidate.result_url)
        else candidate.result_url
    )

    for fetch_url in (
        result_url,
        candidate.url,
        None if candidate.domain.startswith("www.") else f"https://www.{candidate.domain}/",
    ):
        if fetch_url and fetch_url not in fetch_urls:
            fetch_urls.append(fetch_url)

    last_error: FetchError | None = None

    attempts: list[VerificationResult] = []

    for fetch_url in fetch_urls:
        try:
            result = await fetcher.fetch(
                fetch_url,
                force=force,
            )
        except FetchError as exc:
            last_error = exc

            continue

        # Only the attempt that follows the exact profile URL may scope
        # the evidence to its section. The domain main page fallback is
        # always verified as a whole page.
        section_url = fetch_url if fetch_url == result_url else ""

        attempt = _verify_page(
            fund_name=fund_name,
            candidate=candidate,
            page_url=result.final_url,
            content_type=result.content_type,
            body=result.body,
            minimum_score=minimum_score,
            section_url=section_url,
        )

        if attempt.accepted:
            return attempt

        if section_url and urlsplit(section_url).fragment:
            # The section was not sufficient on its own, so the same page
            # is verified once more without scoping.
            whole_page_attempt = _verify_page(
                fund_name=fund_name,
                candidate=candidate,
                page_url=result.final_url,
                content_type=result.content_type,
                body=result.body,
                minimum_score=minimum_score,
                section_url="",
            )

            if whole_page_attempt.accepted:
                return whole_page_attempt

            attempts.append(whole_page_attempt)

        attempts.append(attempt)

    if attempts:
        return max(
            attempts,
            key=lambda item: item.score,
        )

    error_message = (
        f"{last_error.code}: {last_error}" if last_error is not None else "unknown_fetch_error"
    )

    return VerificationResult(
        candidate=candidate,
        page_url=None,
        accepted=False,
        score=0,
        reason=f"The candidate main page could not be downloaded: {error_message}",
        error=error_message,
    )


def _verify_page(
    *,
    fund_name: str,
    candidate: DomainCandidate,
    page_url: str,
    content_type: str | None,
    body: bytes,
    minimum_score: int,
    section_url: str = "",
) -> VerificationResult:
    if not is_html_response(
        content_type=content_type,
        body=body,
    ):
        return VerificationResult(
            candidate=candidate,
            page_url=page_url,
            accepted=False,
            score=0,
            reason="The candidate main page did not return HTML.",
        )

    try:
        parsed_page = parse_html_page(
            body=body,
            page_url=page_url,
        )
    except (
        HtmlDiscoveryError,
        UnicodeDecodeError,
        ValueError,
        RuntimeError,
    ) as exc:
        error_code = getattr(exc, "code", type(exc).__name__)

        return VerificationResult(
            candidate=candidate,
            page_url=page_url,
            accepted=False,
            score=0,
            reason=f"The candidate main page could not be parsed: {exc}",
            error=str(error_code),
        )

    section_id = urlsplit(section_url).fragment if section_url else ""

    content = extract_page_content(
        body=body,
        title=parsed_page.title,
        page_url=(section_url or page_url),
        document_links=[
            (
                item.text,
                item.url,
            )
            for item in parsed_page.candidates
        ],
        section_id=section_id,
    )

    if is_parked_page(
        page_url=page_url,
        content=content,
    ):
        return VerificationResult(
            candidate=candidate,
            page_url=page_url,
            accepted=False,
            score=0,
            reason="The candidate domain is parked or offered for sale.",
            parked=True,
        )

    name_signals = evaluate_name_signals(
        fund_name=fund_name,
        content=content,
        domain=candidate.domain,
    )

    document_signals = evaluate_document_signals(
        parsed_page_candidates=[
            (
                item.document_type,
                item.url,
            )
            for item in parsed_page.candidates
        ],
        visible_text=content.visible_text,
    )

    fund_context = has_fund_context(
        content=content,
        document_signals=document_signals,
    )

    score, evidence = score_signals_with_evidence(
        name_signals=name_signals,
        document_signals=document_signals,
        fund_context=fund_context,
        hostname_rank=hostname_preference(
            fund_name=fund_name,
            domain=candidate.domain,
        ),
    )

    from_adapter = candidate.source == CandidateSourceName.ADAPTER

    structural_support = has_structural_support(
        name_signals=name_signals,
        document_signals=document_signals,
    )

    if not structural_support and not from_adapter:
        evidence.append(
            "+0 the fund name appears only in body text of an ordinary "
            "search result, without a title, heading, URL path, hostname, "
            "manager or document signal"
        )

    accepted = (
        name_signals.identified
        and fund_context
        and score >= minimum_score
        and (structural_support or from_adapter)
    )

    evidence.append(
        f"= {score} against threshold {minimum_score}: {'accepted' if accepted else 'rejected'}"
    )

    return VerificationResult(
        candidate=candidate,
        page_url=page_url,
        accepted=accepted,
        score=score,
        reason=_decision_reason(
            name_signals=name_signals,
            document_signals=document_signals,
            fund_context=fund_context,
            score=score,
            minimum_score=minimum_score,
            from_adapter=from_adapter,
        ),
        name_signals=name_signals,
        document_signals=document_signals,
        fund_context=fund_context,
        evidence=tuple(evidence),
    )


# Elements that carry the title of one fund section. Catalogs present a
# fund as an accordion label or a summary far more often than as a
# document-level heading.
SECTION_HEADING_SELECTORS: Final = "h1, h2, h3, h4, h5, h6, label, summary, legend, caption, th"


def extract_page_content(
    *,
    body: bytes,
    title: str | None,
    page_url: str = "",
    document_links: list[tuple[str, str]] | None = None,
    section_id: str = "",
) -> PageContent:
    """
    Collect every part of a page used as fund name evidence.

    When the candidate addresses one fund section of a shared catalog
    page, the evidence is taken from that section only. A catalog listing
    a hundred funds would otherwise drown the requested one, or push it
    past the text limit entirely.
    """

    headings: list[str] = []

    site_name = ""

    manager_info = ""

    section_text = ""

    section_links: set[str] = set()

    try:
        parser = LexborHTMLParser(decode_html_bytes(body))

        section_node = _find_section_node(
            parser=parser,
            section_id=section_id,
        )

        heading_scope = section_node if section_node is not None else parser

        heading_selectors = SECTION_HEADING_SELECTORS if section_node is not None else "h1, h2"

        for node in heading_scope.css(heading_selectors):
            heading = _node_text(node)

            if heading and heading not in headings:
                headings.append(heading)

        if section_node is not None:
            section_text = _node_text(section_node)

            section_links = {
                node.attributes.get("href") or "" for node in section_node.css("a[href]")
            }

        site_name = _meta_content(
            parser=parser,
            properties=(
                "og:site_name",
                "og:title",
                "application-name",
            ),
        )

        manager_info = " ".join(
            part
            for part in (
                _meta_content(
                    parser=parser,
                    properties=(
                        "description",
                        "og:description",
                    ),
                ),
                " ".join(_node_text(node) for node in parser.css("footer")),
            )
            if part
        )
    except (RuntimeError, ValueError, UnicodeDecodeError):
        headings = []

    scoped_links = _scope_document_links(
        document_links=document_links or [],
        section_links=section_links,
    )

    document_text = " ".join(f"{text} {url}" for text, url in scoped_links)

    return PageContent(
        title=title,
        headings=" ".join(headings),
        site_name=site_name,
        # The section text is short and complete, so it never reaches the
        # truncation applied to a whole catalog page.
        visible_text=(section_text or _visible_text(body)),
        manager_info=manager_info,
        url_path=_url_evidence_path(page_url),
        document_text=document_text,
    )


def _find_section_node(
    *,
    parser: LexborHTMLParser,
    section_id: str,
) -> Any | None:
    """Return the element addressed by a URL fragment, when it exists."""

    if not section_id or '"' in section_id:
        return None

    try:
        nodes = parser.css(f'[id="{section_id}"]')
    except (RuntimeError, ValueError):
        return None

    return nodes[0] if nodes else None


def _scope_document_links(
    *,
    document_links: list[tuple[str, str]],
    section_links: set[str],
) -> list[tuple[str, str]]:
    """Keep only the documents belonging to the verified fund section."""

    if not section_links:
        return document_links

    scoped = [
        (text, url)
        for text, url in document_links
        if any(href and href in url for href in section_links)
    ]

    return scoped or document_links


def _url_evidence_path(
    page_url: str,
) -> str:
    """Return the path and fragment of a URL as name evidence."""

    if not page_url:
        return ""

    parsed = urlsplit(page_url)

    return f"{parsed.path} {parsed.fragment}".strip()


def is_parked_page(
    *,
    page_url: str,
    content: PageContent,
) -> bool:
    """Detect domain parking, marketplace and placeholder pages."""

    final_domain = canonical_domain(page_url)

    if final_domain and is_excluded_domain(final_domain):
        return True

    if any(fragment in page_url.casefold() for fragment in PARKED_DOMAIN_FRAGMENTS):
        return True

    normalized = normalize_search_text(
        " ".join(
            (
                content.title or "",
                content.headings,
                content.visible_text[:2000],
            )
        )
    )

    return any(marker in normalized for marker in PARKED_PAGE_MARKERS)


MAXIMUM_NAME_PHRASES: Final = 40


# Language variants of fund wording. A Czech legal name is regularly
# presented in English on the fund website, for example "Velaris Fund".
PHRASE_WORD_VARIANTS: Final[dict[str, tuple[str, ...]]] = {
    "fond": ("fund", "funds", "fonds"),
    "fund": ("fond",),
    "investicni": ("investment",),
    "spolecnost": ("company",),
}


def name_phrases(
    fund_name: str,
) -> tuple[str, ...]:
    """
    Build multi-word phrases that prove a page names this exact fund.

    Every phrase contains at least two words and all distinctive words of
    the name, so a page sharing only one common word cannot match.
    """

    normalized = normalize_search_text(strip_legal_suffixes(fund_name))

    words = [word for word in re.findall(r"[a-z0-9]+", normalized) if len(word) >= 2]

    if len(words) < 2:
        # A one-word legal name cannot produce a multi-word phrase. The
        # surrounding fund wording of the original name is used instead.
        words = [word for word in re.findall(r"[a-z0-9]+", normalize_search_text(fund_name))]

        words = [word for word in words if len(word) >= 2]

    if len(words) < 2:
        return ()

    distinctive = set(distinctive_name_tokens(fund_name))

    if not distinctive:
        # A name built only from legal and fund wording has no word that
        # identifies it. Only its complete legal name is acceptable
        # evidence, otherwise every fund page would match every such name.
        complete_name = " ".join(words)

        if len(complete_name) < MINIMUM_GENERIC_NAME_LENGTH:
            return ()

        return _phrase_variants(words)

    phrases: list[str] = []

    for start in range(len(words)):
        for end in range(start + 2, len(words) + 1):
            window = words[start:end]

            if distinctive and not distinctive.issubset(set(window)):
                continue

            for variant in _phrase_variants(window):
                if variant not in phrases:
                    phrases.append(variant)

                if len(phrases) >= MAXIMUM_NAME_PHRASES:
                    return tuple(phrases)

    return tuple(phrases)


def _phrase_variants(
    window: list[str],
) -> tuple[str, ...]:
    variants = [" ".join(window)]

    for index, word in enumerate(window):
        for replacement in PHRASE_WORD_VARIANTS.get(word, ()):
            replaced = [*window]

            replaced[index] = replacement

            candidate = " ".join(replaced)

            if candidate not in variants:
                variants.append(candidate)

    return tuple(variants)


def find_name_match(
    *,
    phrases: tuple[str, ...],
    distinctive_tokens: tuple[str, ...],
    haystack: str,
) -> str | None:
    """
    Return the evidence of a strong fund name match in one location.

    Either a complete multi-word phrase is present, or the name carries
    several distinctive words and all of them appear together.
    """

    if not haystack:
        return None

    for phrase in phrases:
        if phrase in haystack:
            return phrase

    if len(distinctive_tokens) >= 2 and all(token in haystack for token in distinctive_tokens):
        return " + ".join(distinctive_tokens)

    return None


def evaluate_name_signals(
    *,
    fund_name: str,
    content: PageContent,
    domain: str,
) -> NameSignals:
    """
    Measure how clearly a page identifies the requested fund.

    The evidence must be a multi-word fund name match found in the page
    title, main heading, URL path, main content, manager information or a
    fund document. A single shared word is never sufficient.
    """

    distinctive_tokens = distinctive_name_tokens(fund_name)

    generic_tokens = generic_name_tokens(fund_name)

    phrases = name_phrases(fund_name)

    # Phrases are built from word tokens, so every location is reduced to
    # word tokens as well. Otherwise "JF Fund SICAV, a.s." on the page
    # would not match the phrase built from the same legal name.
    haystacks: dict[MatchLocation, str] = {
        MatchLocation.TITLE: _word_text(
            " ".join(
                (
                    content.title or "",
                    content.site_name,
                )
            )
        ),
        MatchLocation.HEADING: _word_text(content.headings),
        MatchLocation.URL_PATH: _word_text(content.url_path),
        MatchLocation.CONTENT: _word_text(content.visible_text),
        MatchLocation.MANAGER_INFO: _word_text(content.manager_info),
        MatchLocation.DOCUMENT: _word_text(content.document_text),
    }

    strong_locations: list[MatchLocation] = []

    matched_phrase: str | None = None

    for location, haystack in haystacks.items():
        match = find_name_match(
            phrases=phrases,
            distinctive_tokens=distinctive_tokens,
            haystack=haystack,
        )

        if match is None:
            continue

        strong_locations.append(location)

        if matched_phrase is None:
            matched_phrase = match

    combined_text = " ".join(haystacks.values())

    normalized_full_name = _word_text(strip_legal_suffixes(fund_name))

    exact_name_match = (
        len(normalized_full_name) >= MINIMUM_GENERIC_NAME_LENGTH
        and normalized_full_name in combined_text
    )

    normalized_domain = normalize_search_text(domain)

    return NameSignals(
        distinctive_tokens=distinctive_tokens,
        generic_tokens=generic_tokens,
        distinctive_coverage=_coverage(
            tokens=distinctive_tokens,
            haystack=combined_text,
        ),
        generic_coverage=_coverage(
            tokens=generic_tokens,
            haystack=combined_text,
        ),
        strong_locations=tuple(strong_locations),
        matched_phrase=matched_phrase,
        exact_name_match=exact_name_match,
        hostname_token_match=any(token in normalized_domain for token in distinctive_tokens),
    )


def evaluate_document_signals(
    *,
    parsed_page_candidates: list[tuple[DocumentType, str]],
    visible_text: str,
) -> DocumentSignals:
    """Detect relevant fund documents linked or mentioned on a page."""

    document_types: list[str] = []

    document_urls: list[str] = []

    has_document_section = False

    for document_type, url in parsed_page_candidates:
        if document_type in RELEVANT_DOCUMENT_TYPES:
            if document_type.value not in document_types:
                document_types.append(document_type.value)

            if url not in document_urls:
                document_urls.append(url)

            continue

        has_document_section = True

    normalized_text = normalize_search_text(visible_text)

    text_references = tuple(
        keyword for keyword in RELEVANT_TEXT_KEYWORDS if keyword in normalized_text
    )

    if not has_document_section:
        has_document_section = any(
            keyword in normalized_text for keyword in DOCUMENT_SECTION_KEYWORDS
        )

    return DocumentSignals(
        document_types=tuple(document_types),
        document_urls=tuple(document_urls),
        text_references=text_references,
        has_document_section=has_document_section,
    )


def has_structural_support(
    *,
    name_signals: NameSignals,
    document_signals: DocumentSignals,
) -> bool:
    """
    Return whether the name match rests on more than plain body text.

    A page listing many funds contains any fund name in its body text,
    so that alone cannot verify ownership of a website.
    """

    if set(name_signals.strong_locations) & STRUCTURAL_MATCH_LOCATIONS:
        return True

    if name_signals.hostname_token_match:
        return True

    return bool(document_signals.document_types)


def has_fund_context(
    *,
    content: PageContent,
    document_signals: DocumentSignals,
) -> bool:
    """
    Return whether the page presents a fund at all.

    This replaces the previous hard document requirement. A fund website
    without a document link on its homepage still qualifies, while an
    unrelated company sharing a name token does not.
    """

    if document_signals.present or document_signals.has_document_section:
        return True

    normalized = normalize_search_text(
        " ".join(
            (
                content.title or "",
                content.site_name,
                content.headings,
                content.visible_text,
            )
        )
    )

    return any(keyword in normalized for keyword in FUND_CONTEXT_KEYWORDS)


def score_signals_with_evidence(
    *,
    name_signals: NameSignals,
    document_signals: DocumentSignals,
    fund_context: bool,
    hostname_rank: int = 2,
) -> tuple[int, list[str]]:
    """
    Combine the signals into one score and explain every contribution.

    The returned lines make it possible to review why a candidate was
    accepted or rejected without repeating the request.
    """

    score = 0

    evidence: list[str] = []

    def add(
        points: int,
        explanation: str,
    ) -> None:
        nonlocal score

        score += points

        evidence.append(f"{points:+d} {explanation}")

    if name_signals.strong_locations:
        best_location = min(
            name_signals.strong_locations,
            key=lambda location: -LOCATION_SCORES[location],
        )

        add(
            LOCATION_SCORES[best_location],
            f'name match "{name_signals.matched_phrase}" in {best_location.value}',
        )

        additional = [
            location for location in name_signals.strong_locations if location is not best_location
        ]

        if additional:
            add(
                min(
                    SCORE_ADDITIONAL_LOCATION * len(additional),
                    SCORE_ADDITIONAL_LOCATION_MAXIMUM,
                ),
                ("confirmed in " + ", ".join(location.value for location in additional)),
            )
    else:
        evidence.append(
            "+0 no multi-word fund name match in title, heading, URL path, "
            "content, manager information or documents"
        )

    if name_signals.exact_name_match:
        add(
            SCORE_EXACT_NAME_BONUS,
            "complete legal fund name present",
        )

    distinctive_count = len(name_signals.distinctive_tokens)

    if distinctive_count > 1 and name_signals.distinctive_coverage < 1.0:
        matched = round(name_signals.distinctive_coverage * distinctive_count)

        add(
            -PENALTY_PARTIAL_NAME,
            (
                f"only {matched} of {distinctive_count} distinctive name "
                "words matched, which is typical of an unrelated website"
            ),
        )

    if hostname_rank == 0:
        add(
            SCORE_HOSTNAME_FUND_TOKEN,
            "hostname combines the fund name with fund wording",
        )
    elif hostname_rank == 1 or name_signals.hostname_token_match:
        add(
            SCORE_HOSTNAME_TOKEN,
            "hostname contains a distinctive fund name word",
        )

    generic_points = int(name_signals.generic_coverage * SCORE_GENERIC_TOKEN_MAXIMUM)

    if generic_points:
        add(
            generic_points,
            "supporting legal and fund wording of the name present",
        )

    if fund_context:
        add(
            SCORE_FUND_CONTEXT,
            "page presents a fund rather than an unrelated company",
        )
    else:
        evidence.append("+0 no fund presentation wording found on the page")

    if document_signals.document_types:
        document_score = SCORE_DOCUMENT_LINK + SCORE_DOCUMENT_LINK_EXTRA * (
            len(document_signals.document_types) - 1
        )

        add(
            min(
                document_score,
                SCORE_DOCUMENT_LINK_MAXIMUM,
            ),
            ("fund documents linked: " + ", ".join(document_signals.document_types)),
        )
    elif document_signals.text_references:
        add(
            SCORE_DOCUMENT_TEXT_REFERENCE,
            "fund documents mentioned in the page text",
        )
    elif document_signals.has_document_section:
        add(
            SCORE_DOCUMENT_SECTION,
            "a document section is linked",
        )

    return (
        score,
        evidence,
    )


def score_signals(
    *,
    name_signals: NameSignals,
    document_signals: DocumentSignals,
    fund_context: bool,
    hostname_rank: int = 2,
) -> int:
    """Combine name, context and document signals into one score."""

    score, _ = score_signals_with_evidence(
        name_signals=name_signals,
        document_signals=document_signals,
        fund_context=fund_context,
        hostname_rank=hostname_rank,
    )

    return score


def _decision_reason(
    *,
    name_signals: NameSignals,
    document_signals: DocumentSignals,
    fund_context: bool,
    score: int,
    minimum_score: int,
    from_adapter: bool = False,
) -> str:
    if not name_signals.identified:
        missing = ", ".join(name_signals.distinctive_tokens) or "the fund name"

        return (
            "The main page does not clearly identify the fund: no "
            "multi-word fund name match was found in the title, heading, "
            "URL path, content, manager information or documents "
            f"(distinctive coverage {name_signals.distinctive_coverage:.2f} "
            f"for: {missing})."
        )

    if not fund_context:
        return (
            "The main page contains the fund name but nothing indicating "
            "a fund presentation, so it is probably an unrelated company."
        )

    if score < minimum_score:
        return f"The combined verification score {score} is below the required {minimum_score}."

    if not from_adapter and not has_structural_support(
        name_signals=name_signals,
        document_signals=document_signals,
    ):
        return (
            "The fund name appears only in the body text of an ordinary "
            "search result. A title, heading, URL path, hostname, manager "
            "or document signal is required to accept a website."
        )

    locations = ", ".join(location.value for location in name_signals.strong_locations)

    documents = ", ".join(document_signals.document_types) or "no linked documents"

    return (
        f'The page identifies the fund by "{name_signals.matched_phrase}" '
        f"in {locations} ({documents}) with score {score}."
    )


def _word_text(
    value: str,
) -> str:
    """Reduce text to lowercase word tokens separated by single spaces."""

    normalized = normalize_search_text(value)

    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _coverage(
    *,
    tokens: tuple[str, ...],
    haystack: str,
) -> float:
    if not tokens or not haystack:
        return 0.0

    matched = sum(1 for token in tokens if token in haystack)

    return matched / len(tokens)


def _contains_all(
    *,
    tokens: tuple[str, ...],
    haystack: str,
) -> bool:
    if not tokens or not haystack:
        return False

    return all(token in haystack for token in tokens)


def _visible_text(
    body: bytes,
) -> str:
    try:
        document = parse_markup_document(
            body=body,
            document_format=DocumentFormat.HTML,
        )
    except DocumentParseError:
        return ""

    return document.full_text[:MAXIMUM_VERIFIED_TEXT_CHARACTERS]


def _meta_content(
    *,
    parser: LexborHTMLParser,
    properties: tuple[str, ...],
) -> str:
    values: list[str] = []

    for node in parser.css("meta[content]"):
        key = (node.attributes.get("property") or node.attributes.get("name") or "").casefold()

        if key not in properties:
            continue

        content = node.attributes.get("content") or ""

        if content and content not in values:
            values.append(content)

    return " ".join(values)


def _node_text(
    node: Any,
) -> str:
    text = node.text(
        separator=" ",
        strip=True,
    )

    return " ".join(str(text).split())


@dataclass(eq=False)
class DomainVerificationCache:
    """
    Verify each candidate page only once per run for one fund name.

    The key contains the fund name and the exact candidate URL. Keying on
    the domain alone would let a rejected homepage block the later
    verification of the correct fund profile on the same domain.
    """

    results: dict[tuple[str, str, str], VerificationResult] = field(default_factory=dict)

    locks: dict[tuple[str, str, str], asyncio.Lock] = field(default_factory=dict)

    async def verify(
        self,
        *,
        fund_name: str,
        candidate: DomainCandidate,
        fetcher: HttpFetcher,
        minimum_score: int = DEFAULT_MINIMUM_SCORE,
        force: bool = False,
    ) -> VerificationResult:
        key = (
            normalize_search_text(fund_name),
            candidate.domain,
            canonical_url(candidate.result_url or candidate.url),
        )

        lock = self.locks.setdefault(
            key,
            asyncio.Lock(),
        )

        async with lock:
            cached_result = self.results.get(key)

            if cached_result is not None:
                return cached_result

            result = await verify_candidate(
                fund_name=fund_name,
                candidate=candidate,
                fetcher=fetcher,
                minimum_score=minimum_score,
                force=force,
            )

            self.results[key] = result

            return result
