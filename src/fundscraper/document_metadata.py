"""
What is known about one parsed document before any value is read.

The three questions that have to be answered first - what kind of
document is this, whose is it, and when does it apply - are answered here
in one place, from the parsed pages the previous step produced. The
answer is stored so that a wrong value can later be traced back to the
document it came from and to the reason that document was trusted.

Nothing is extracted here. The result is the ground an extraction stands
on, and a document whose scope was refused is meant to be skipped rather
than mined.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fundscraper.database import (
    DatabaseError,
    DocumentMetadataRecord,
    ParsedDocumentRecord,
    record_document_metadata,
)
from fundscraper.document_classification import (
    ClassificationSignals,
    DocumentClassification,
    classify_document,
)
from fundscraper.document_dates import DocumentDates, extract_document_dates
from fundscraper.document_identity import (
    DocumentIdentity,
    DocumentScope,
    IdentityInput,
    resolve_document_identity,
)
from fundscraper.document_parser import ParsedDocument

# How many leading lines of a page count as its running head. A fund
# repeats its name there on every page, which is the strongest identity
# evidence a long document offers.
HEADER_LINES: int = 3


# How many pages are sampled for the repeated header. Reading them all
# would add nothing: a running head that appears on ten pages appears on
# the rest as well.
HEADER_PAGES: int = 12


# How much of the first page counts as the title page.
TITLE_CHARACTERS: int = 2_000


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """The type, the owner and the dates of one document."""

    source_id: int
    url: str
    classification: DocumentClassification
    identity: DocumentIdentity
    dates: DocumentDates
    parser_name: str
    pages_parsed: int
    ocr_used: bool
    warnings: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """Return whether values may be read from this document."""

        return self.identity.is_accepted


def describe_document(
    *,
    record: ParsedDocumentRecord,
    document: ParsedDocument,
    fund_name: str,
    fund_ico: str | None = None,
) -> DocumentFacts:
    """Answer what one parsed document is, whose it is and when it applies."""

    pages = document.pages

    title_text = pages[0].text[:TITLE_CHARACTERS] if pages else ""

    repeated_text = repeated_header_text(document)

    body_text = document.full_text

    classification = classify_document(
        ClassificationSignals(
            url=record.url,
            anchor_text=record.title or "",
            document_title=_first_line(title_text),
            header_text=title_text,
            body_text=body_text,
        )
    )

    identity = resolve_document_identity(
        IdentityInput(
            fund_name=fund_name,
            fund_ico=fund_ico,
            url=record.url,
            title_text=title_text,
            repeated_text=repeated_text,
            body_text=body_text,
        )
    )

    dates = extract_document_dates(
        text=body_text,
        url=record.url,
    )

    warnings: list[str] = []

    if classification.ambiguous:
        warnings.append(
            f"document type is ambiguous between "
            f"{classification.document_type.value} and "
            f"{classification.runner_up.value if classification.runner_up else 'unknown'}"
        )

    if document.scanned_candidate:
        warnings.append("document looks scanned and may need optical recognition")

    if identity.scope is DocumentScope.AMBIGUOUS:
        warnings.append("identity evidence contradicts itself")

    return DocumentFacts(
        source_id=record.source_id,
        url=record.url,
        classification=classification,
        identity=identity,
        dates=dates,
        parser_name=document.parser_name,
        pages_parsed=document.page_count,
        ocr_used=document.parser_name.endswith("+ocr"),
        warnings=tuple(warnings),
    )


def repeated_header_text(
    document: ParsedDocument,
) -> str:
    """
    Return the lines that repeat at the top of the pages.

    A running head is the one place a long document names its fund on
    every page, and it survives even when the title page is an image.
    """

    if document.page_count < 2:
        return ""

    counted: dict[str, int] = {}

    for page in document.pages[:HEADER_PAGES]:
        for line in page.text.splitlines()[:HEADER_LINES]:
            cleaned = " ".join(line.split())

            if len(cleaned) < 4:
                continue

            counted[cleaned] = counted.get(cleaned, 0) + 1

    repeated = [line for line, occurrences in counted.items() if occurrences > 1]

    return "\n".join(repeated)


def store_document_facts(
    *,
    database_path: Path,
    fund_id: str,
    facts: DocumentFacts,
    now: datetime | None = None,
) -> None:
    """Persist what was learned about one document."""

    identity = facts.identity

    dates = facts.dates

    record_document_metadata(
        database_path,
        DocumentMetadataRecord(
            source_id=facts.source_id,
            fund_id=fund_id,
            document_type=(facts.classification.document_type.value),
            type_score=facts.classification.score,
            type_ambiguous=(facts.classification.ambiguous),
            type_evidence=_as_json(
                {
                    "keywords": list(facts.classification.matched_keywords),
                    "signals": list(facts.classification.signals_used),
                    "runner_up": (
                        facts.classification.runner_up.value
                        if facts.classification.runner_up
                        else None
                    ),
                }
            ),
            scope=identity.scope.value,
            scope_confidence=identity.confidence,
            scope_accepted=identity.is_accepted,
            scope_rejection_reason=(identity.rejection_reason),
            identity_evidence=_as_json(
                [
                    {
                        "kind": item.kind,
                        "detail": item.detail,
                        "supports_fund": item.supports_fund,
                    }
                    for item in identity.evidence
                ]
            ),
            subfund_name=identity.subfund_name,
            share_class=identity.share_class,
            matched_ico=identity.matched_ico,
            matched_isin=identity.matched_isin,
            published_at=_date_text(dates.published_at),
            published_at_origin=(dates.published_at.origin.value if dates.published_at else None),
            effective_at=_date_text(dates.effective_at),
            reporting_period_start=(_date_text(dates.reporting_period_start)),
            reporting_period_end=(_date_text(dates.reporting_period_end)),
            as_of=_date_text(dates.as_of),
            parser_name=facts.parser_name,
            ocr_used=facts.ocr_used,
            pages_parsed=facts.pages_parsed,
            parse_warnings=("; ".join(facts.warnings) if facts.warnings else None),
        ),
        now=now,
    )


def store_document_facts_quietly(
    *,
    database_path: Path,
    fund_id: str,
    facts: DocumentFacts,
) -> str | None:
    """Persist the facts, returning the failure instead of raising it."""

    try:
        store_document_facts(
            database_path=database_path,
            fund_id=fund_id,
            facts=facts,
        )
    except DatabaseError as exc:
        return f"document metadata: {facts.url}: {exc}"

    return None


def _first_line(
    text: str,
) -> str:
    for line in text.splitlines():
        cleaned = " ".join(line.split())

        if len(cleaned) >= 3:
            return cleaned

    return ""


def _date_text(
    value: object,
) -> str | None:
    if value is None:
        return None

    return str(getattr(value, "value", "")) or None


def _as_json(
    payload: object,
) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
    )
