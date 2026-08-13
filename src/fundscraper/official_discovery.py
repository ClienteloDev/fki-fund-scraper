"""
Official-source discovery for one fund.

The stage explores the official website of a fund, or the profile of the
fund on the website of its manager, before any external source is
consulted. It walks the navigation, the investor and document sections,
the fund and subfund pages, and the sitemaps the site publishes, and it
collects the official documents it finds along the way.

Every page and document it looks at is recorded with the reason it was
accepted or refused, so a fund with a missing document can be explained
from the log rather than by crawling it again.

The stage discovers; it does not download documents. What it returns is
handed to the existing crawler as seeds, which keeps the download,
parsing and extraction paths unchanged.
"""

from __future__ import annotations

import heapq
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from urllib.parse import unquote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from fundscraper.conflict_resolution import MANAGER_HOST_FRAGMENTS
from fundscraper.database import (
    DatabaseError,
    DiscoveryLogRecord,
    record_discovery_entry,
)
from fundscraper.discovery_priority import (
    DOCUMENT_THRESHOLD,
    GENERIC_NAME_TOKENS,
    NAVIGATION_THRESHOLD,
    DiscoveryMethod,
    PrioritySignals,
    distinctive_tokens,
    document_type_rank,
    file_name_of,
    score_link,
)
from fundscraper.html_discovery import (
    DiscoveredLink,
    classify_link,
    decode_html_bytes,
    is_direct_document_url,
    is_html_response,
    normalize_search_text,
    parse_html_page,
    resolve_link_url,
)
from fundscraper.http_client import FetchError, FetchResult, HttpFetcher
from fundscraper.models import FundInput
from fundscraper.normalization import canonical_domain, canonical_url
from fundscraper.output_models import DocumentType
from fundscraper.output_service import stable_fund_id
from fundscraper.run_diagnostics import (
    Diagnostic,
    DiagnosticLevel,
    classify_fetch_failure,
    escalate_repeated_server_errors,
)
from fundscraper.site_crawler import anchor_text as read_anchor_text
from fundscraper.sitemap_discovery import (
    MAXIMUM_SITEMAP_DEPTH,
    MAXIMUM_SITEMAP_DOCUMENTS,
    candidate_sitemap_urls,
    filter_sitemap_entries,
    is_allowed,
    is_sitemap_address,
    parse_sitemap,
    robots_disallowed_paths,
    robots_sitemap_urls,
    robots_url,
)

# Why a discovered link was not accepted for this fund.
REJECTED_OFF_DOMAIN = "off_domain"
REJECTED_LOW_PRIORITY = "low_priority"
REJECTED_UNRELATED = "unrelated_page"
REJECTED_ROBOTS = "disallowed_by_robots"
REJECTED_OTHER_FUND = "belongs_to_another_fund"
REJECTED_OTHER_FUND_DOCUMENT = "other_fund_document"
REJECTED_DUPLICATE = "duplicate_document"
REJECTED_BUDGET = "crawl_budget_exhausted"
REJECTED_FETCH_FAILED = "fetch_failed"


# How a document was attributed to the fund.
SCOPE_EXACT_FUND = "exact_fund"
SCOPE_OWN_DOMAIN = "own_official_domain"
SCOPE_FUND_SECTION = "fund_section_of_manager_site"


# Wording that turns a name into the name of a fund. Without one of these
# a document called "Statut fondu" is simply the statute of whatever fund
# the page belongs to, and must not be read as naming a different one.
OTHER_FUND_MARKERS: tuple[str, ...] = (
    "sicav",
    "podfond",
    "subfund",
    "investicni fond",
    "otevreny podilovy fond",
    "uzavreny investicni fond",
)


# Words that describe a document rather than a fund. They are removed
# before what remains is treated as somebody's name, otherwise "Statut
# fondu SICAV" would look as if it named a fund called "statut".
DOCUMENT_VOCABULARY: frozenset[str] = frozenset(
    {
        "statut",
        "statute",
        "stanovy",
        "vyrocni",
        "pololetni",
        "zprava",
        "zpravy",
        "zpravu",
        "report",
        "annual",
        "kid",
        "kiid",
        "priips",
        "klicove",
        "klicovych",
        "informace",
        "informaci",
        "sdeleni",
        "memorandum",
        "prospekt",
        "prospectus",
        "factsheet",
        "list",
        "ucetni",
        "zaverka",
        "cenik",
        "dokument",
        "dokumenty",
        "dokumentu",
        "archiv",
        "trida",
        "tridy",
        "tride",
        "class",
        "pdf",
        "download",
        "soubor",
        "verze",
        "aktualni",
        "platny",
        "cze",
        "eng",
    }
)


# A token has to be this long before it is compared as a fragment. It
# lets the subfund "ESG SeniorCare" match the fund "CARE SICAV" without
# letting two unrelated three-letter codes match each other.
FRAGMENT_MATCH_LENGTH: int = 4


# What the official sources have to cover before an external source
# becomes pointless. Each group is satisfied by any one of its members,
# because a fund publishes the same facts under different names: the
# fees live in the statute or in the key information document, and the
# assets live in an annual report, a statement or a factsheet.
#
# Having found one of these is not the same as having found them all.
# A site that publishes only a key information document still leaves the
# assets and the reporting history unanswered, which is exactly what the
# fallback adapters exist to supply.
REQUIRED_DOCUMENT_GROUPS: tuple[
    tuple[
        str,
        frozenset[DocumentType],
    ],
    ...,
] = (
    (
        "key_information",
        frozenset({DocumentType.PRIIPS_KID}),
    ),
    (
        "governing_document",
        frozenset(
            {
                DocumentType.STATUTE,
                DocumentType.SUBFUND_STATUTE,
                DocumentType.MEMORANDUM,
            }
        ),
    ),
    (
        "reporting_document",
        frozenset(
            {
                DocumentType.ANNUAL_REPORT,
                DocumentType.FINANCIAL_STATEMENTS,
                DocumentType.HALF_YEAR_REPORT,
                DocumentType.FACTSHEET,
            }
        ),
    ),
)


# The document types a fund publishes about itself. One of them found on
# a site that runs many funds, with nothing naming this fund, is another
# fund's document: a key information document and a statute always belong
# to exactly one fund. A price list, an investor notice or a corporate
# page can legitimately cover every fund a manager runs, so they are not
# in this set and are never refused by the identity rule.
FUND_SPECIFIC_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset(
    {
        DocumentType.PRIIPS_KID,
        DocumentType.STATUTE,
        DocumentType.SUBFUND_STATUTE,
        DocumentType.MEMORANDUM,
        DocumentType.PROSPECTUS,
        DocumentType.FACTSHEET,
        DocumentType.ANNUAL_REPORT,
        DocumentType.HALF_YEAR_REPORT,
        DocumentType.FINANCIAL_STATEMENTS,
    }
)


# Puts one URL into the crawl frontier at the given priority.
type Enqueue = Callable[..., None]


@dataclass(frozen=True, slots=True)
class DiscoveryEntry:
    """One page or document the stage looked at."""

    url: str
    method: DiscoveryMethod
    discovered_from: str | None
    priority_score: int
    document_type: DocumentType | None
    title: str | None
    scope_decision: str | None
    accepted: bool
    rejection_reason: str | None
    is_document: bool


@dataclass(frozen=True, slots=True)
class OfficialDiscoveryMetrics:
    """What the stage did for one fund."""

    pages_inspected: int = 0
    pages_fetched: int = 0
    documents_discovered: int = 0
    documents_accepted: int = 0
    documents_rejected: int = 0
    official_source_hits: int = 0
    sitemaps_read: int = 0
    budget_exhausted: bool = False
    method_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OfficialDiscoveryResult:
    """The official sources found for one fund."""

    fund_id: str
    fund_name: str
    official_domain: str
    navigation_urls: tuple[str, ...]
    documents: tuple[DiscoveredLink, ...]
    entries: tuple[DiscoveryEntry, ...]
    metrics: OfficialDiscoveryMetrics
    warnings: tuple[str, ...]

    # Everything the stage recorded, with an expected miss told apart
    # from something that went wrong. ``warnings`` keeps only the lines
    # that are worth a reader's attention.
    diagnostics: tuple[Diagnostic, ...] = ()

    # The document types this pass was sent out to find, if any. They
    # count towards sufficiency next to the groups every fund needs.
    wanted_document_types: frozenset[DocumentType] = frozenset()

    @property
    def is_exhausted(self) -> bool:
        """
        Return whether the official site was explored to the end.

        This says nothing about what was found. It reports only that the
        crawl ran out of pages to visit rather than out of budget, so a
        caller can tell an incomplete exploration from a complete one
        that simply had little to offer.
        """

        return not self.metrics.budget_exhausted

    @property
    def missing_document_groups(self) -> tuple[str, ...]:
        """
        Return the document groups and types the official site lacks.

        A pass sent out for a particular field asks for every document
        type that could answer it, and any one of them does. Listing
        each unfound type separately would report a fund as short of
        nine documents when the one it needed was found.
        """

        found = {document.document_type for document in self.documents}

        missing = [name for name, accepted in REQUIRED_DOCUMENT_GROUPS if not (found & accepted)]

        if self.wanted_document_types and not (found & self.wanted_document_types):
            missing.append(
                "wanted:" + "|".join(sorted(item.value for item in self.wanted_document_types))
            )

        return tuple(missing)

    @property
    def is_sufficient(self) -> bool:
        """
        Return whether the official sources answer what is required.

        Only a complete set makes an external fallback pointless. One
        key information document on its own does not, however cleanly
        the stage finished.
        """

        return not self.missing_document_groups


async def discover_official_sources(
    *,
    database_path: Path | None,
    fund: FundInput,
    fetcher: HttpFetcher,
    max_pages: int = 18,
    max_documents: int = 60,
    force: bool = False,
    respect_robots: bool = True,
    run_id: str = "",
    # Step 7. The document types this pass was sent out to find, and
    # whether it may stop as soon as it holds a usable set.
    wanted_document_types: frozenset[DocumentType] = frozenset(),
    stop_when_sufficient: bool = False,
) -> OfficialDiscoveryResult:
    """
    Explore the official site of one fund and collect its documents.

    ``wanted_document_types`` raises the priority of the documents that
    would answer a particular open field, and adds them to what counts as
    a sufficient result. ``stop_when_sufficient`` ends the walk once the
    required groups are covered, which is what keeps the first pass of a
    two-pass run cheap on the funds that publish properly.
    """

    fund_id = stable_fund_id(fund)

    if not fund.web:
        # Nothing official is known to explore. The fallback adapters are
        # the only route to such a fund, and reporting the stage as
        # insufficient is what lets them run.
        return OfficialDiscoveryResult(
            fund_id=fund_id,
            fund_name=fund.name,
            official_domain="",
            navigation_urls=(),
            documents=(),
            entries=(),
            metrics=OfficialDiscoveryMetrics(),
            warnings=("no official website is known for this fund",),
            diagnostics=(
                Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    stage="discovery",
                    code="no_official_website",
                    message=("no official website is known for this fund"),
                ),
            ),
        )

    official_domain = canonical_domain(fund.web)

    entries: list[DiscoveryEntry] = []

    diagnostics: list[Diagnostic] = []

    documents: dict[str, DiscoveredLink] = {}

    document_scopes: dict[str, str] = {}

    visited: set[str] = set()

    queued: set[str] = set()

    pages_fetched = 0

    sitemaps_read = 0

    method_counts: Counter[str] = Counter()

    # A priority queue keeps the crawl on the pages most likely to hold
    # documents. A plain breadth-first walk spends the budget on whatever
    # the main navigation happens to list first.
    order = count()

    frontier: list[tuple[int, int, str, DiscoveryMethod, str | None]] = []

    def enqueue(
        *,
        url: str,
        score: int,
        method: DiscoveryMethod,
        discovered_from: str | None,
    ) -> None:
        key = canonical_url(url)

        if key in queued or key in visited:
            return

        queued.add(key)

        heapq.heappush(
            frontier,
            (
                -score,
                next(order),
                url,
                method,
                discovered_from,
            ),
        )

    def take_sitemap_document(
        *,
        url: str,
        score: int,
        discovered_from: str | None,
    ) -> None:
        """Judge a document the sitemap listed, without fetching it."""

        key = canonical_url(url)

        if key in visited:
            return

        visited.add(key)

        if respect_robots and not is_allowed(
            url=url,
            disallowed_paths=disallowed,
        ):
            entries.append(
                _entry(
                    url=url,
                    method=DiscoveryMethod.SITEMAP,
                    discovered_from=discovered_from,
                    score=score,
                    accepted=False,
                    rejection_reason=REJECTED_ROBOTS,
                    is_document=True,
                )
            )

            return

        _consider_document(
            link_url=url,
            anchor_text="",
            page_url=discovered_from or fund.web or "",
            page_title="",
            score_value=score,
            names_the_fund=bool(
                score_link(
                    PrioritySignals(
                        url=url,
                        fund_name=fund.name,
                    )
                ).matched_fund_tokens
            ),
            fund=fund,
            documents=documents,
            document_scopes=document_scopes,
            entries=entries,
            max_documents=max_documents,
            method=DiscoveryMethod.SITEMAP,
        )

    disallowed: tuple[str, ...] = ()

    guessed_addresses: set[str] = set()

    robots_address = robots_url(fund.web)

    if robots_address is not None:
        robots_result = await _try_fetch(
            fetcher=fetcher,
            url=robots_address,
            force=force,
            diagnostics=diagnostics,
            guessed=True,
        )

        if robots_result is not None:
            if respect_robots:
                disallowed = robots_disallowed_paths(body=robots_result.body)

            for sitemap_address in robots_sitemap_urls(
                body=robots_result.body,
                base_url=robots_address,
            ):
                enqueue(
                    url=sitemap_address,
                    score=200,
                    method=DiscoveryMethod.SITEMAP,
                    discovered_from=robots_address,
                )

    for sitemap_address in candidate_sitemap_urls(fund.web):
        # Invented, not linked. A site that does not publish it owes
        # nobody an explanation, so its 404 is an expected miss.
        guessed_addresses.add(canonical_url(sitemap_address))

        enqueue(
            url=sitemap_address,
            score=190,
            method=DiscoveryMethod.SITEMAP,
            discovered_from=None,
        )

    enqueue(
        url=fund.web,
        score=1_000,
        method=DiscoveryMethod.SEED,
        discovered_from=None,
    )

    sitemap_depth: dict[str, int] = {}

    while frontier and pages_fetched < max_pages:
        negative_score, _, url, method, discovered_from = heapq.heappop(frontier)

        key = canonical_url(url)

        if key in visited:
            continue

        visited.add(key)

        score = -negative_score

        is_sitemap = method is DiscoveryMethod.SITEMAP and not _looks_like_page(url)

        if respect_robots and not is_allowed(
            url=url,
            disallowed_paths=disallowed,
        ):
            entries.append(
                _entry(
                    url=url,
                    method=method,
                    discovered_from=discovered_from,
                    score=score,
                    accepted=False,
                    rejection_reason=REJECTED_ROBOTS,
                )
            )

            continue

        fetched = await _try_fetch(
            fetcher=fetcher,
            url=url,
            force=force,
            diagnostics=diagnostics,
            guessed=canonical_url(url) in guessed_addresses,
        )

        if fetched is None:
            entries.append(
                _entry(
                    url=url,
                    method=method,
                    discovered_from=discovered_from,
                    score=score,
                    accepted=False,
                    rejection_reason=REJECTED_FETCH_FAILED,
                )
            )

            continue

        pages_fetched += 1

        method_counts[method.value] += 1

        body = fetched.body

        # A site that redirects a bare host to its www form serves the
        # homepage for every path. Resolving the links of the page
        # against the address that was requested rather than the one
        # that answered rebuilt those broken addresses over and over,
        # and the whole document section of such a site was lost.
        page_url = fetched.final_url

        visited.add(canonical_url(page_url))

        if is_sitemap and sitemaps_read < MAXIMUM_SITEMAP_DOCUMENTS:
            sitemaps_read += 1

            _read_sitemap(
                body=body,
                url=page_url,
                fund=fund,
                official_domain=official_domain,
                depth=sitemap_depth.get(
                    canonical_url(url),
                    0,
                ),
                sitemap_depth=sitemap_depth,
                enqueue=enqueue,
                on_document=take_sitemap_document,
                entries=entries,
            )

            continue

        if not is_html_response(
            content_type=None,
            body=body,
        ):
            entries.append(
                _entry(
                    url=url,
                    method=method,
                    discovered_from=discovered_from,
                    score=score,
                    accepted=True,
                    scope_decision=SCOPE_OWN_DOMAIN,
                )
            )

            continue

        entries.append(
            _entry(
                url=url,
                method=method,
                discovered_from=discovered_from,
                score=score,
                accepted=True,
                scope_decision=SCOPE_OWN_DOMAIN,
            )
        )

        page_title = _page_title(body)

        for link_url, link_anchor in _iter_links(
            body=body,
            page_url=page_url,
        ):
            _consider_link(
                link_url=link_url,
                anchor_text=link_anchor,
                page_url=page_url,
                page_title=page_title,
                fund=fund,
                official_domain=official_domain,
                documents=documents,
                document_scopes=document_scopes,
                entries=entries,
                enqueue=enqueue,
                max_documents=max_documents,
                wanted_document_types=wanted_document_types,
            )

        if stop_when_sufficient and _covers_what_is_needed(
            documents=documents,
            wanted_document_types=wanted_document_types,
        ):
            # The site has already answered every required group. Walking
            # the rest of it would only find further copies of what is
            # in hand, which Step 8 showed is where conflicts come from.
            break

    budget_exhausted = bool(frontier)

    for _, _, url, method, discovered_from in frontier:
        entries.append(
            _entry(
                url=url,
                method=method,
                discovered_from=discovered_from,
                score=0,
                accepted=False,
                rejection_reason=REJECTED_BUDGET,
            )
        )

    ordered_documents = tuple(
        sorted(
            documents.values(),
            key=lambda item: (
                -item.score,
                -document_type_rank(item.document_type),
                item.url,
            ),
        )
    )

    # A sitemap has already been read by this stage. Handing it to the
    # crawler as a page made it fetch the file again, store it as a
    # source and send it to the document parser: eleven per cent of all
    # parsed documents in a full run were sitemaps, one of them 140 000
    # characters wide and parsed once per fund of a shared manager
    # domain.
    navigation_urls = tuple(
        entry.url
        for entry in entries
        if entry.accepted and not entry.is_document and not is_sitemap_address(entry.url)
    )

    metrics = OfficialDiscoveryMetrics(
        pages_inspected=len(visited),
        pages_fetched=pages_fetched,
        documents_discovered=sum(1 for entry in entries if entry.is_document),
        documents_accepted=len(ordered_documents),
        documents_rejected=sum(1 for entry in entries if entry.is_document and not entry.accepted),
        official_source_hits=len(ordered_documents),
        sitemaps_read=sitemaps_read,
        budget_exhausted=budget_exhausted,
        method_counts=dict(sorted(method_counts.items())),
    )

    # One document linked from both the Czech and the English version of
    # a page is seen twice. The second sighting is a duplicate, but the
    # discovery log is keyed by URL and upserts, so leaving both in made
    # the later "duplicate, rejected" row overwrite the earlier accepted
    # one — a factsheet that was found and downloaded was recorded as
    # refused. The entries are collapsed before anything reads them.
    entries = _collapse_entries(entries)

    if database_path is not None:
        _persist(
            database_path=database_path,
            fund_id=fund_id,
            entries=entries,
            run_id=run_id,
            diagnostics=diagnostics,
        )

    recorded = escalate_repeated_server_errors(diagnostics)

    return OfficialDiscoveryResult(
        fund_id=fund_id,
        fund_name=fund.name,
        official_domain=official_domain,
        navigation_urls=navigation_urls,
        documents=ordered_documents,
        entries=tuple(entries),
        metrics=metrics,
        # An expected miss is not worth a reader's attention, so it stays
        # out of the warning lines while remaining counted in diagnostics.
        warnings=tuple(
            item.rendered() for item in recorded if item.level is not DiagnosticLevel.EXPECTED_MISS
        ),
        diagnostics=tuple(recorded),
        wanted_document_types=wanted_document_types,
    )


def _collapse_entries(
    entries: list[DiscoveryEntry],
) -> list[DiscoveryEntry]:
    """
    Keep one entry per address, the one that says what became of it.

    An address that was accepted once was accepted, however many later
    sightings were skipped as duplicates. Reporting the last sighting
    instead of the decisive one understated what discovery found.
    """

    best: dict[str, DiscoveryEntry] = {}

    for entry in entries:
        key = canonical_url(entry.url)

        current = best.get(key)

        if current is None or (entry.accepted and not current.accepted):
            best[key] = entry

    return list(best.values())


def _covers_what_is_needed(
    *,
    documents: dict[str, DiscoveredLink],
    wanted_document_types: frozenset[DocumentType],
) -> bool:
    """
    Return whether the documents in hand answer what the pass came for.

    Every required group has to be present, and at least one of the
    document types the caller asked for by name. A pass sent out for
    annual reports is not finished because it found a key information
    document, but it is finished once it holds one of the several kinds
    of report that would answer the same field.
    """

    found = {document.document_type for document in documents.values()}

    if any(not (found & accepted) for _, accepted in REQUIRED_DOCUMENT_GROUPS):
        return False

    return not wanted_document_types or bool(found & wanted_document_types)


def _consider_link(
    *,
    link_url: str,
    anchor_text: str,
    page_url: str,
    page_title: str,
    fund: FundInput,
    official_domain: str,
    documents: dict[str, DiscoveredLink],
    document_scopes: dict[str, str],
    entries: list[DiscoveryEntry],
    enqueue: Enqueue,
    max_documents: int,
    wanted_document_types: frozenset[DocumentType] = frozenset(),
) -> None:
    """Decide what one link found on an official page is worth."""

    signals = PrioritySignals(
        url=link_url,
        anchor_text=anchor_text,
        page_title=page_title,
        fund_name=fund.name,
    )

    score = score_link(
        signals,
        wanted_document_types=wanted_document_types,
    )

    named_type = _named_document_type(
        link_url=link_url,
        anchor_text=anchor_text,
        page_url=page_url,
    )

    # A document does not have to end in ".pdf". A fund site that serves
    # its statute from "/file/sdff-get?id=7371" states what the file is
    # in the link text alone, and refusing such a link left the whole
    # document section of a manager site undiscovered.
    is_document = is_direct_document_url(link_url) or named_type is not None

    if canonical_domain(link_url) != official_domain:
        # The official stage never leaves the official domain. An
        # external host is what the fallback adapters exist for.
        entries.append(
            _entry(
                url=link_url,
                method=(
                    DiscoveryMethod.DOCUMENT_LINK if is_document else DiscoveryMethod.NAVIGATION
                ),
                discovered_from=page_url,
                score=score.value,
                accepted=False,
                rejection_reason=REJECTED_OFF_DOMAIN,
                is_document=is_document,
                title=anchor_text or None,
            )
        )

        return

    if is_document:
        _consider_document(
            link_url=link_url,
            anchor_text=anchor_text,
            page_url=page_url,
            page_title=page_title,
            score_value=score.value,
            names_the_fund=score.names_the_fund,
            fund=fund,
            documents=documents,
            document_scopes=document_scopes,
            entries=entries,
            max_documents=max_documents,
        )

        return

    if score.value < NAVIGATION_THRESHOLD:
        entries.append(
            _entry(
                url=link_url,
                method=DiscoveryMethod.NAVIGATION,
                discovered_from=page_url,
                score=score.value,
                accepted=False,
                rejection_reason=(
                    REJECTED_UNRELATED
                    if "unrelated_page" in score.reasons
                    else REJECTED_LOW_PRIORITY
                ),
                title=anchor_text or None,
            )
        )

        return

    enqueue(
        url=link_url,
        score=score.value,
        method=DiscoveryMethod.NAVIGATION,
        discovered_from=page_url,
    )


def _named_document_type(
    *,
    link_url: str,
    anchor_text: str,
    page_url: str,
) -> DocumentType | None:
    """
    Return the document a link names, when it names one at all.

    Only a recognised official type counts. The generic "documents"
    wording of a section link classifies as OTHER, which keeps a link to
    a document archive a page to crawl rather than a file to download.
    """

    if not anchor_text.strip():
        return None

    classified = classify_link(
        url=link_url,
        text=anchor_text,
        page_url=page_url,
    )

    if classified is None or classified.document_type is DocumentType.OTHER:
        return None

    return classified.document_type


def _consider_document(
    *,
    link_url: str,
    anchor_text: str,
    page_url: str,
    page_title: str,
    score_value: int,
    names_the_fund: bool,
    fund: FundInput,
    documents: dict[str, DiscoveredLink],
    document_scopes: dict[str, str],
    entries: list[DiscoveryEntry],
    max_documents: int,
    method: DiscoveryMethod = DiscoveryMethod.DOCUMENT_LINK,
) -> None:
    """Decide whether one document belongs to this fund."""

    # The type is read from the file name as well as from the anchor.
    # A link whose text is only "Stáhnout" still says what it is in its
    # address, and "sdeleni-klicovych-informaci-2026.pdf" only matches
    # the keywords once its separators are spaces.
    classified = classify_link(
        url=link_url,
        text=f"{anchor_text} {page_title} {file_name_of(link_url)}",
        page_url=page_url,
    )

    document_type = classified.document_type if classified is not None else DocumentType.OTHER

    key = canonical_url(link_url)

    decision = _scope_of(
        fund=fund,
        link_url=link_url,
        page_url=page_url,
        page_title=page_title,
        anchor_text=anchor_text,
        names_the_fund=names_the_fund,
        document_type=document_type,
    )

    scope = decision.scope

    if scope is None:
        # Refused before the download, so the bytes are never fetched and
        # the document never reaches the parser.
        entries.append(
            _entry(
                url=link_url,
                method=method,
                discovered_from=page_url,
                score=score_value,
                accepted=False,
                rejection_reason=(decision.rejection_reason or REJECTED_OTHER_FUND),
                document_type=document_type,
                is_document=True,
                title=anchor_text or None,
            )
        )

        return

    if score_value < DOCUMENT_THRESHOLD and document_type is DocumentType.OTHER:
        entries.append(
            _entry(
                url=link_url,
                method=method,
                discovered_from=page_url,
                score=score_value,
                accepted=False,
                rejection_reason=REJECTED_LOW_PRIORITY,
                document_type=document_type,
                is_document=True,
                title=anchor_text or None,
            )
        )

        return

    if key in documents:
        # The same document is linked from the overview and from the
        # detail page of a fund. It is one document, not two.
        existing = documents[key]

        if score_value > existing.score:
            documents[key] = _as_link(
                url=link_url,
                anchor_text=anchor_text,
                score=score_value,
                document_type=document_type,
            )

            document_scopes[key] = scope

        entries.append(
            _entry(
                url=link_url,
                method=method,
                discovered_from=page_url,
                score=score_value,
                accepted=False,
                rejection_reason=REJECTED_DUPLICATE,
                document_type=document_type,
                scope_decision=scope,
                is_document=True,
                title=anchor_text or None,
            )
        )

        return

    if len(documents) >= max_documents:
        entries.append(
            _entry(
                url=link_url,
                method=method,
                discovered_from=page_url,
                score=score_value,
                accepted=False,
                rejection_reason=REJECTED_BUDGET,
                document_type=document_type,
                scope_decision=scope,
                is_document=True,
                title=anchor_text or None,
            )
        )

        return

    documents[key] = _as_link(
        url=link_url,
        anchor_text=anchor_text,
        score=score_value,
        document_type=document_type,
    )

    document_scopes[key] = scope

    entries.append(
        _entry(
            url=link_url,
            method=method,
            discovered_from=page_url,
            score=score_value,
            accepted=True,
            document_type=document_type,
            scope_decision=scope,
            is_document=True,
            title=anchor_text or None,
        )
    )


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """How one document was attributed, or why it could not be."""

    scope: str | None = None
    rejection_reason: str | None = None


def _hosts_many_funds(
    fund: FundInput,
) -> bool:
    """
    Return whether the official address is the site of a fund manager.

    A manager runs many funds from one host, so the absence of this
    fund's name on a document there is not neutral. The hosts are the
    ones the ranking rules already know, so there is one list rather
    than two.
    """

    host = canonical_domain(fund.web or "")

    return any(fragment in host for fragment in MANAGER_HOST_FRAGMENTS)


def _scope_of(
    *,
    fund: FundInput,
    link_url: str,
    page_url: str,
    page_title: str,
    anchor_text: str,
    names_the_fund: bool,
    document_type: DocumentType = DocumentType.OTHER,
) -> ScopeDecision:
    """
    Return how a document was attributed, or why it cannot be.

    A fund with its own website owns everything on it. A fund hosted on
    the website of its manager owns only what its own section says, which
    is why a manager domain needs the name of the fund somewhere in the
    page, the link or the address.
    """

    official_domain = canonical_domain(fund.web or "")

    page_domain = canonical_domain(page_url)

    if page_domain != official_domain:
        return ScopeDecision(rejection_reason=REJECTED_OFF_DOMAIN)

    if _link_names_another_fund(
        fund=fund,
        link_url=link_url,
        anchor_text=anchor_text,
    ):
        # The page belongs to this fund but the link does not. A manager
        # puts the statute of a neighbouring fund on a fund page often
        # enough that inheriting the attribution of the page is wrong.
        return ScopeDecision(rejection_reason=REJECTED_OTHER_FUND)

    if names_the_fund:
        return ScopeDecision(scope=SCOPE_EXACT_FUND)

    page_signals = PrioritySignals(
        url=page_url,
        anchor_text=anchor_text,
        page_title=page_title,
        fund_name=fund.name,
    )

    if score_link(page_signals).names_the_fund:
        return ScopeDecision(scope=SCOPE_FUND_SECTION)

    if not _is_single_fund_site(fund):
        return ScopeDecision(rejection_reason=REJECTED_OTHER_FUND)

    if _hosts_many_funds(fund) and document_type in FUND_SPECIFIC_DOCUMENT_TYPES:
        # The address of this fund is the bare site of a manager that
        # runs many of them, so "own domain" is not the fund's own: the
        # host is shared with every other fund the manager administers.
        # A key information document or a statute belongs to exactly one
        # fund, and nothing here names this one, so it is somebody
        # else's until it says otherwise. Downloading these cost the
        # first pass more than half of its parsing time and produced
        # values the scope rules then threw away.
        #
        # A price list or an investor notice is not refused: a manager
        # publishes one that covers every fund it runs, and the absence
        # of this fund's name says nothing against it.
        return ScopeDecision(rejection_reason=REJECTED_OTHER_FUND_DOCUMENT)

    return ScopeDecision(scope=SCOPE_OWN_DOMAIN)


def _link_names_another_fund(
    *,
    fund: FundInput,
    link_url: str,
    anchor_text: str,
) -> bool:
    """
    Return whether a link names a fund other than this one.

    Only a link that carries fund wording is judged. What is left after
    the document vocabulary is removed is the name it refers to, and a
    name sharing nothing with this fund belongs to a different one.

    A fragment counts as a match so that the subfund "ESG SeniorCare"
    is still recognised as part of "CARE SICAV, a.s.".
    """

    text = normalize_search_text(
        " ".join(
            (
                anchor_text,
                _readable_url(link_url),
            )
        )
    )

    if not any(marker in text for marker in OTHER_FUND_MARKERS):
        return False

    fund_tokens = set(distinctive_tokens(fund.name))

    if not fund_tokens:
        return False

    named_tokens = {
        token
        for token in re.findall(
            r"[a-z]{3,}",
            text,
        )
        if token not in GENERIC_NAME_TOKENS and token not in DOCUMENT_VOCABULARY
    }

    if not named_tokens:
        return False

    return not any(
        _tokens_match(
            named=named,
            expected=expected,
        )
        for named in named_tokens
        for expected in fund_tokens
    )


def _tokens_match(
    *,
    named: str,
    expected: str,
) -> bool:
    if named == expected:
        return True

    if len(expected) >= FRAGMENT_MATCH_LENGTH and expected in named:
        return True

    return len(named) >= FRAGMENT_MATCH_LENGTH and named in expected


def _readable_url(
    url: str,
) -> str:
    """Return the address of a link with its separators as spaces."""

    parts = urlsplit(url)

    return re.sub(
        r"[-_/.+]",
        " ",
        unquote(f"{parts.path} {parts.query}"),
    )


def _is_single_fund_site(
    fund: FundInput,
) -> bool:
    """
    Return whether the official address belongs to this fund alone.

    The address of a fund with its own site points at the site root or at
    a short path. A profile on a manager site carries the fund in a deep
    path, which is what separates the two without a list of managers.
    """

    parts = urlsplit(fund.web or "")

    segments = [segment for segment in parts.path.split("/") if segment]

    if not segments:
        return True

    tokens = distinctive_tokens(fund.name)

    host = normalize_search_text(parts.netloc.replace("-", " "))

    return bool(tokens) and any(token in host for token in tokens)


def _read_sitemap(
    *,
    body: bytes,
    url: str,
    fund: FundInput,
    official_domain: str,
    depth: int,
    sitemap_depth: dict[str, int],
    enqueue: Enqueue,
    on_document: Enqueue,
    entries: list[DiscoveryEntry],
) -> None:
    """Turn one sitemap into further sitemaps and crawlable pages."""

    document = parse_sitemap(
        body=body,
        sitemap_url=url,
    )

    if document.nested_sitemaps and depth < MAXIMUM_SITEMAP_DEPTH:
        for nested in document.nested_sitemaps:
            sitemap_depth[canonical_url(nested)] = depth + 1

            enqueue(
                url=nested,
                score=180 - depth,
                method=DiscoveryMethod.SITEMAP,
                discovered_from=url,
            )

    relevant = filter_sitemap_entries(
        entries=document.entries,
        fund_name=fund.name,
        official_domain=official_domain,
    )

    for item in relevant:
        if item.is_document:
            # A sitemap that lists a PDF has already delivered the
            # document. Queueing it as a page would fetch it and then
            # drop it, because a PDF carries no links to follow.
            on_document(
                url=item.url,
                score=item.score,
                discovered_from=url,
            )

            continue

        enqueue(
            url=item.url,
            score=item.score,
            method=DiscoveryMethod.SITEMAP,
            discovered_from=url,
        )

    entries.append(
        _entry(
            url=url,
            method=DiscoveryMethod.SITEMAP,
            discovered_from=None,
            score=0,
            accepted=True,
            scope_decision=(f"sitemap:{len(document.entries)} urls, {len(relevant)} relevant"),
        )
    )


def _iter_links(
    *,
    body: bytes,
    page_url: str,
) -> list[tuple[str, str]]:
    """Return every resolvable link of one page with its anchor text."""

    try:
        parsed = parse_html_page(
            body=body,
            page_url=page_url,
        )

        base_url = parsed.effective_base_url
    except Exception:
        base_url = page_url

    parser = LexborHTMLParser(decode_html_bytes(body))

    links: list[tuple[str, str]] = []

    seen: set[str] = set()

    for node in parser.css("a[href]"):
        raw_href = node.attributes.get("href")

        if not raw_href:
            continue

        resolved = resolve_link_url(
            base_url=base_url,
            raw_href=raw_href,
        )

        if resolved is None:
            continue

        key = canonical_url(resolved)

        if key in seen or key == canonical_url(page_url):
            continue

        seen.add(key)

        links.append(
            (
                resolved,
                read_anchor_text(node),
            )
        )

    return links


def _page_title(
    body: bytes,
) -> str:
    parser = LexborHTMLParser(decode_html_bytes(body))

    nodes = parser.css("title")

    if not nodes:
        return ""

    return nodes[0].text(strip=True) or ""


def _looks_like_page(
    url: str,
) -> bool:
    """Return whether a URL queued as a sitemap is really a page."""

    return (
        not urlsplit(url)
        .path.casefold()
        .endswith(
            (
                ".xml",
                ".xml.gz",
                "/robots.txt",
            )
        )
    )


def _as_link(
    *,
    url: str,
    anchor_text: str,
    score: int,
    document_type: DocumentType,
) -> DiscoveredLink:
    return DiscoveredLink(
        url=url,
        text=anchor_text,
        score=score,
        document_type=document_type,
        same_domain=True,
        direct_document=True,
    )


def _entry(
    *,
    url: str,
    method: DiscoveryMethod,
    discovered_from: str | None,
    score: int,
    accepted: bool,
    rejection_reason: str | None = None,
    document_type: DocumentType | None = None,
    scope_decision: str | None = None,
    is_document: bool = False,
    title: str | None = None,
) -> DiscoveryEntry:
    return DiscoveryEntry(
        url=url,
        method=method,
        discovered_from=discovered_from,
        priority_score=score,
        document_type=document_type,
        title=title,
        scope_decision=scope_decision,
        accepted=accepted,
        rejection_reason=rejection_reason,
        is_document=is_document,
    )


async def _try_fetch(
    *,
    fetcher: HttpFetcher,
    url: str,
    force: bool,
    diagnostics: list[Diagnostic],
    guessed: bool = False,
) -> FetchResult | None:
    """
    Fetch one address, recording what happened when it does not answer.

    ``guessed`` says the address was invented by discovery rather than
    followed from a link, which is what makes a 404 an expected answer
    instead of a problem.
    """

    try:
        return await fetcher.fetch(
            url,
            force=force,
        )
    except FetchError as exc:
        diagnostics.append(
            Diagnostic(
                level=classify_fetch_failure(
                    url=url,
                    code=exc.code,
                    status_code=getattr(exc, "status_code", None),
                    guessed=guessed,
                ),
                stage="discovery",
                code=exc.code,
                message=str(exc),
                url=url,
            )
        )

        return None


def _persist(
    *,
    database_path: Path,
    fund_id: str,
    entries: list[DiscoveryEntry],
    run_id: str,
    diagnostics: list[Diagnostic],
) -> None:
    for entry in entries:
        try:
            record_discovery_entry(
                database_path,
                DiscoveryLogRecord(
                    fund_id=fund_id,
                    url=entry.url,
                    method=entry.method.value,
                    discovered_from=entry.discovered_from,
                    priority_score=entry.priority_score,
                    document_type=(
                        entry.document_type.value if entry.document_type is not None else None
                    ),
                    title=entry.title,
                    scope_decision=entry.scope_decision,
                    accepted=entry.accepted,
                    rejection_reason=entry.rejection_reason,
                    is_document=entry.is_document,
                    run_id=run_id,
                ),
            )
        except DatabaseError as exc:
            diagnostics.append(
                Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    stage="discovery log",
                    code="database_error",
                    message=str(exc),
                    url=entry.url,
                )
            )

            return
