"""
What to crawl, how much of it, and for which funds.

Crawling every fund as deeply as the hardest one is the wrong trade: most
funds publish their key information document, their statute and a report
on the first page a visitor lands on, and spending forty page fetches to
confirm it buys nothing. The funds that need a deep crawl are the ones
whose fields came out missing, doubtful or contested, and they are known
only after a first pass has run.

This module holds the decisions, not the crawling. It says how large each
pass may be, which funds earned a second one and why, which document
types would answer their open questions, and which of several copies of
one document is the one worth keeping. The crawling itself stays in the
Step 5 official-first discovery and the existing site crawler.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from fundscraper.discovery_priority import DOCUMENT_TYPE_RANK, file_name_of
from fundscraper.html_discovery import DiscoveredLink
from fundscraper.output_models import DocumentType


class CrawlPass(StrEnum):
    """Which of the two passes a fund is being crawled in."""

    FAST = "fast"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class CrawlBudget:
    """How much one pass may spend on a single fund."""

    official_max_pages: int
    official_max_documents: int
    max_pages: int
    max_depth: int
    max_documents: int
    document_concurrency: int = 4

    # Whether the official stage may stop as soon as it holds a usable
    # set of documents instead of exploring the site to the end.
    stop_when_sufficient: bool = False


# The first pass visits every fund, so its budget is set by what a fund
# that publishes properly actually needs: the landing page, the investor
# or documents section, a sitemap, and the documents they link to. The
# 341-fund audit found the required document groups on the official site
# of most funds well inside this.
FAST_BUDGET: Final = CrawlBudget(
    official_max_pages=8,
    official_max_documents=25,
    max_pages=10,
    max_depth=1,
    max_documents=12,
    document_concurrency=4,
    stop_when_sufficient=True,
)


# The second pass runs only for funds that came out of the first one with
# an open question, so it can afford to walk archives, year folders and
# subfund pages that the fast pass deliberately skipped.
DEEP_BUDGET: Final = CrawlBudget(
    official_max_pages=40,
    official_max_documents=120,
    max_pages=45,
    max_depth=3,
    max_documents=40,
    document_concurrency=4,
    stop_when_sufficient=False,
)


# Which documents answer which field. A fund missing its minimum
# investment needs the documents that state subscription terms, not more
# annual reports, and a fund missing its assets needs the opposite.
FIELD_DOCUMENT_TYPES: Final[dict[str, frozenset[DocumentType]]] = {
    "investment_horizon": frozenset(
        {
            DocumentType.PRIIPS_KID,
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
            DocumentType.MEMORANDUM,
        }
    ),
    "minimum_investment": frozenset(
        {
            DocumentType.PRIIPS_KID,
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
            DocumentType.MEMORANDUM,
            DocumentType.FACTSHEET,
        }
    ),
    "target_return": frozenset(
        {
            DocumentType.PRIIPS_KID,
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
            DocumentType.MEMORANDUM,
            DocumentType.FACTSHEET,
        }
    ),
    "fees": frozenset(
        {
            DocumentType.PRIIPS_KID,
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
            DocumentType.PRICE_LIST,
            DocumentType.MEMORANDUM,
        }
    ),
    "assets_under_management": frozenset(
        {
            DocumentType.ANNUAL_REPORT,
            DocumentType.FINANCIAL_STATEMENTS,
            DocumentType.HALF_YEAR_REPORT,
            DocumentType.FACTSHEET,
        }
    ),
    "aum_history": frozenset(
        {
            DocumentType.ANNUAL_REPORT,
            DocumentType.FINANCIAL_STATEMENTS,
            DocumentType.HALF_YEAR_REPORT,
            DocumentType.FACTSHEET,
        }
    ),
    "annual_returns": frozenset(
        {
            DocumentType.ANNUAL_REPORT,
            DocumentType.FACTSHEET,
            DocumentType.PRIIPS_KID,
            DocumentType.INFOLETTER,
        }
    ),
    "historical_values": frozenset(
        {
            DocumentType.FACTSHEET,
            DocumentType.INFOLETTER,
            DocumentType.ANNUAL_REPORT,
            DocumentType.FINANCIAL_STATEMENTS,
        }
    ),
    "manager": frozenset(
        {
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
            DocumentType.PRIIPS_KID,
            DocumentType.ANNUAL_REPORT,
        }
    ),
    "administrator": frozenset(
        {
            DocumentType.STATUTE,
            DocumentType.SUBFUND_STATUTE,
            DocumentType.PRIIPS_KID,
            DocumentType.ANNUAL_REPORT,
        }
    ),
}


# The fields whose state may send a fund into the deep pass. News is
# deliberately absent: an announcement page nobody publishes is not a
# reason to spend forty page fetches on a website.
TRIGGER_FIELDS: Final[tuple[str, ...]] = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
)


# The delivered field states that mean the fund still owes an answer.
UNANSWERED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "pending",
        "not_found",
        "ambiguous",
        "conflicting",
        "error",
    }
)


# The audit verdicts that mean the delivered value cannot be trusted as
# it stands. A suspicious value is deliberately not one of them on its
# own: most suspicious findings are about how a number was read, not
# about the source being missing, and re-crawling would not change them.
DOUBTED_AUDIT_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "conflicting",
        "rejected",
    }
)


# Suspicious findings that a better source really could fix, because each
# of them says the value came from the wrong document rather than that it
# was read wrongly.
SOURCE_FIXABLE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "missing_source",
        "missing_evidence",
        "third_party_source",
        "source_does_not_name_the_fund",
        "source_shared_across_funds",
        "stale_as_of_date",
        "manager_level_scope",
        "scope_mismatch",
        "only_manager_level_data",
    }
)


class DeepPassTrigger(StrEnum):
    """Why one field sent its fund into the deep pass."""

    FIELD_UNANSWERED = "field_unanswered"
    AUDIT_REJECTED = "audit_rejected"
    AUDIT_CONFLICTING = "audit_conflicting"
    SOURCE_FIXABLE_FINDING = "source_fixable_finding"
    UNRESOLVED_BETWEEN_SOURCES = "unresolved_between_sources"


@dataclass(frozen=True, slots=True)
class DeepPassReason:
    """One field, and what is wrong with it."""

    field: str
    trigger: DeepPassTrigger
    detail: str


@dataclass(frozen=True, slots=True)
class DeepPassPlan:
    """The funds that earned a second pass, and what to look for."""

    fund_id: str
    fund_name: str
    reasons: tuple[DeepPassReason, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        seen: list[str] = []

        for reason in self.reasons:
            if reason.field not in seen:
                seen.append(reason.field)

        return tuple(seen)

    @property
    def wanted_document_types(self) -> frozenset[DocumentType]:
        """Return the document types that would answer the open fields."""

        wanted: set[DocumentType] = set()

        for field_name in self.fields:
            wanted |= FIELD_DOCUMENT_TYPES.get(
                field_name,
                frozenset(),
            )

        return frozenset(wanted)


def select_deep_pass_funds(
    *,
    output_records: Sequence[dict[str, Any]],
    audit_findings: Sequence[dict[str, Any]] = (),
    conflict_records: Sequence[dict[str, Any]] = (),
) -> list[DeepPassPlan]:
    """
    Decide which funds are worth crawling a second time, and why.

    Three inputs are read, and each of them answers a different question.
    The delivered output says which fields have no answer at all. The
    Step 4 audit says which delivered answers are wrong or contradictory.
    The Step 8 conflict report says which fields two different sources
    disagree about — and, importantly, which ones only look contested
    because one document was read twice, which no amount of crawling can
    fix.
    """

    reasons_by_fund: dict[str, list[DeepPassReason]] = {}

    names_by_fund: dict[str, str] = {}

    for record in output_records:
        fund_id = str(record.get("fund_id") or "")

        if not fund_id:
            continue

        names_by_fund[fund_id] = str(record.get("name") or "")

        for field_name in TRIGGER_FIELDS:
            payload = record.get(field_name)

            if not isinstance(payload, dict):
                continue

            status = str(payload.get("status") or "")

            if status not in UNANSWERED_STATUSES:
                continue

            reasons_by_fund.setdefault(
                fund_id,
                [],
            ).append(
                DeepPassReason(
                    field=field_name,
                    trigger=DeepPassTrigger.FIELD_UNANSWERED,
                    detail=f"The delivered field is {status}.",
                )
            )

    for finding in audit_findings:
        field_name = str(finding.get("field") or "")

        if field_name not in TRIGGER_FIELDS:
            continue

        fund_id = str(finding.get("fund_id") or "")

        if not fund_id:
            continue

        names_by_fund.setdefault(
            fund_id,
            str(finding.get("fund_name") or ""),
        )

        status = str(finding.get("status") or "")

        reason_code = str(finding.get("reason_code") or "")

        trigger = _audit_trigger(
            status=status,
            reason_code=reason_code,
        )

        if trigger is None:
            continue

        reasons_by_fund.setdefault(
            fund_id,
            [],
        ).append(
            DeepPassReason(
                field=field_name,
                trigger=trigger,
                detail=f"{status}: {reason_code}",
            )
        )

    ids_by_name = {name: fund_id for fund_id, name in names_by_fund.items() if name}

    for conflict in conflict_records:
        field_name = str(conflict.get("field") or "")

        if field_name not in TRIGGER_FIELDS:
            continue

        if str(conflict.get("outcome") or "") != "unresolved":
            continue

        if not conflict_is_between_sources(conflict):
            # Two readings of one document. A wider crawl would return
            # the same page and the same two readings of it, so the fund
            # is not sent back out for this.
            continue

        matched_id = ids_by_name.get(str(conflict.get("fund_name") or ""))

        if matched_id is None:
            continue

        fund_id = matched_id

        reasons_by_fund.setdefault(
            fund_id,
            [],
        ).append(
            DeepPassReason(
                field=field_name,
                trigger=(DeepPassTrigger.UNRESOLVED_BETWEEN_SOURCES),
                detail=str(conflict.get("semantic_key") or field_name),
            )
        )

    return [
        DeepPassPlan(
            fund_id=fund_id,
            fund_name=names_by_fund.get(
                fund_id,
                "",
            ),
            reasons=tuple(reasons),
        )
        for fund_id, reasons in sorted(
            reasons_by_fund.items(),
            key=lambda item: (
                -len(item[1]),
                item[0],
            ),
        )
        if reasons
    ]


def conflict_is_between_sources(
    conflict: dict[str, Any],
) -> bool:
    """Return whether a conflict really involves two different sources."""

    alternatives = conflict.get("alternatives")

    if not isinstance(alternatives, list):
        return False

    urls = {str(item.get("source_url") or "") for item in alternatives if isinstance(item, dict)}

    return len(urls - {""}) > 1


def _audit_trigger(
    *,
    status: str,
    reason_code: str,
) -> DeepPassTrigger | None:
    if status == "rejected":
        return DeepPassTrigger.AUDIT_REJECTED

    if status == "conflicting":
        return DeepPassTrigger.AUDIT_CONFLICTING

    if status == "suspicious" and reason_code in SOURCE_FIXABLE_REASONS:
        return DeepPassTrigger.SOURCE_FIXABLE_FINDING

    return None


# ---------------------------------------------------------------------------
# One authoritative copy per document
# ---------------------------------------------------------------------------


# A fund has exactly one statute and one key information document in
# force at a time. Older revisions of them say what used to be true, and
# reading both is how a fund ends up with two different fee schedules.
SINGLE_REVISION_TYPES: Final[frozenset[DocumentType]] = frozenset(
    {
        DocumentType.PRIIPS_KID,
        DocumentType.STATUTE,
        DocumentType.SUBFUND_STATUTE,
        DocumentType.MEMORANDUM,
        DocumentType.PROSPECTUS,
        DocumentType.PRICE_LIST,
    }
)


_YEAR_PATTERN: Final = re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)")


@dataclass(frozen=True, slots=True)
class DocumentSelection:
    """Which copies of a document were kept and which were passed over."""

    kept: tuple[DiscoveredLink, ...]
    superseded: tuple[DiscoveredLink, ...]


def select_authoritative_documents(
    documents: Iterable[DiscoveredLink],
) -> DocumentSelection:
    """
    Keep one copy of each document instead of every copy of it.

    Step 8 found that most disagreements between sources are a fund's own
    statute or key information document published twice, once on its site
    and once on its manager's, or one revision sitting next to its
    predecessor. Downloading all of them costs bandwidth and then costs a
    conflict, so one copy is kept per document and revision.

    A reporting document is treated differently: an annual report of 2022
    and one of 2024 are different facts, not two copies of one, and the
    extended fields are built out of exactly that history. Only two
    copies of the same year are collapsed.
    """

    best: dict[tuple[str, str], DiscoveredLink] = {}

    superseded: list[DiscoveredLink] = []

    for document in sorted(
        documents,
        key=_document_strength,
        reverse=True,
    ):
        key = _revision_key(document)

        current = best.get(key)

        if current is None:
            best[key] = document

            continue

        superseded.append(document)

    return DocumentSelection(
        kept=tuple(
            sorted(
                best.values(),
                key=lambda item: (
                    -item.score,
                    item.url,
                ),
            )
        ),
        superseded=tuple(superseded),
    )


def _revision_key(
    document: DiscoveredLink,
) -> tuple[str, str]:
    """Return what makes two links copies of the same document."""

    document_type = document.document_type

    if document_type in SINGLE_REVISION_TYPES:
        # Every revision of a statute is a copy of the same document for
        # this purpose: only the one in force is wanted.
        return (
            document_type.value,
            "current",
        )

    return (
        document_type.value,
        _document_year(document) or _document_slug(document),
    )


def _document_year(
    document: DiscoveredLink,
) -> str:
    """Return the year a document reports on, read from its address."""

    years = [
        match.group("year") for match in _YEAR_PATTERN.finditer(f"{document.url} {document.text}")
    ]

    return max(years) if years else ""


def _document_slug(
    document: DiscoveredLink,
) -> str:
    """Return the file name of an undated document, as its identity."""

    return file_name_of(document.url)


def _document_strength(
    document: DiscoveredLink,
) -> tuple[int, int, str]:
    """Order copies so that the newest and best-typed one is kept."""

    return (
        int(_document_year(document) or 0),
        document.score + DOCUMENT_TYPE_RANK.get(document.document_type, 0),
        document.url,
    )
