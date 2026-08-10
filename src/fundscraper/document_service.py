from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fundscraper.anydoc_fallback import (
    FallbackDecision,
    FallbackOutcome,
    apply_layout_fallback,
    extracted_field_names,
)
from fundscraper.database import (
    AttemptStatus,
    DatabaseError,
    ParsedDocumentRecord,
    SourceRecord,
    list_parseable_sources,
    record_attempt,
    record_parsed_document,
)
from fundscraper.document_metadata import (
    DocumentFacts,
    describe_document,
    store_document_facts_quietly,
)
from fundscraper.document_parser import (
    DocumentFormat,
    DocumentParseError,
    ParsedDocument,
    load_parsed_document,
    ocr_available,
    parse_document,
    write_parsed_document,
)
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id

# What the parse the layout fallback replaced is stored under. The
# pipeline reads the unsuffixed file; this one is kept so the page-numbered
# reading of the same source is never lost.
PRIMARY_PARSE_SUFFIX = ".primary"


@dataclass(frozen=True, slots=True)
class DocumentParsingFailure:
    source_id: int
    url: str
    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class DocumentParsingSummary:
    fund_id: str
    fund_name: str
    sources_considered: int
    documents_parsed: int
    scanned_candidates: int
    total_characters: int
    failures: tuple[DocumentParsingFailure, ...]

    # What was learned about the documents before any value was read.
    documents_classified: int = 0
    documents_with_published_date: int = 0
    documents_with_effective_date: int = 0
    scope_counts: tuple[tuple[str, int], ...] = ()
    document_type_counts: tuple[tuple[str, int], ...] = ()
    parser_counts: tuple[tuple[str, int], ...] = ()
    ocr_candidates: int = 0
    ocr_used: int = 0
    ocr_available: bool = False

    # What the selective layout fallback did. It is reported next to the
    # parse counts rather than inside them, because every one of these
    # documents was parsed by the primary parser first.
    anydoc_triggered: int = 0
    anydoc_accepted: int = 0
    anydoc_rejected: int = 0
    anydoc_blocked: int = 0
    anydoc_failed: int = 0
    anydoc_seconds: float = 0.0


def _verification_record(
    *,
    source: SourceRecord,
    fund_id: str,
    document: ParsedDocument,
) -> ParsedDocumentRecord:
    """
    Describe one reading the way the extraction expects to receive it.

    This record never reaches the database. It exists so the two readings
    of a document can be put in front of the extraction and compared by
    what it gets out of them.
    """

    return ParsedDocumentRecord(
        source_id=source.source_id,
        fund_id=fund_id,
        url=source.url,
        title=None,
        document_type=source.document_type,
        content_type=source.content_type,
        retrieved_at=None,
        document_format=document.document_format.value,
        parser_name=document.parser_name,
        page_count=document.page_count,
        character_count=document.character_count,
        scanned_candidate=document.scanned_candidate,
        text_path="",
        # The extraction dates its evidence from this, and it is only ever
        # compared with itself here, so the time of the comparison is
        # enough and always parses.
        parsed_at=datetime.now(UTC).isoformat(),
        local_path=source.local_path,
    )


def _parse_source_document(
    *,
    source: SourceRecord,
    parsed_directory: Path,
    fund_id: str,
    fund_name: str,
    allow_ocr: bool = False,
    allow_anydoc_fallback: bool = False,
) -> tuple[ParsedDocument, Path, FallbackDecision]:
    """
    Read, parse and validate one source outside the event-loop thread.

    The document is parsed normally first. Optical recognition is only
    attempted when that produced a document with no usable text, so a
    PDF that reads perfectly well is never sent through it.

    ``allow_anydoc_fallback`` permits a second reading of a PDF whose
    layout defeated the parser. It never runs first and never runs on a
    document the primary parser handled well, and the primary parse is
    kept on disk either way.
    """

    if source.local_path is None:
        raise DocumentParseError("Downloaded source does not contain a local file path")

    source_path = Path(source.local_path)

    if not source_path.exists():
        raise DocumentParseError(f"Downloaded source file does not exist: {source_path}")

    body = source_path.read_bytes()

    document = parse_document(
        body=body,
        content_type=source.content_type,
        url=source.url,
    )

    if allow_ocr and _needs_recognition(document) and ocr_available():
        recognised = parse_document(
            body=body,
            content_type=source.content_type,
            url=source.url,
            allow_ocr=True,
        )

        # The recognised copy is only kept when it actually read more
        # than the normal parser did. The original result stays the
        # fallback, so a failed recognition never loses what was there.
        if recognised.character_count > document.character_count:
            document = recognised

    decision = FallbackDecision(
        outcome=FallbackOutcome.NOT_TRIGGERED,
        document=document,
    )

    if allow_anydoc_fallback:
        decision = apply_layout_fallback(
            body=body,
            primary=document,
            document_type=source.document_type,
            fund_name=fund_name,
            verify=lambda reading: extracted_field_names(
                document=reading,
                record=_verification_record(
                    source=source,
                    fund_id=fund_id,
                    document=reading,
                ),
                fund_name=fund_name,
            ),
        )

    if decision.accepted and decision.document is not None:
        # The primary parse keeps its own file. Its pages carry the page
        # numbers every quote is checked against, and a fallback that
        # later turns out to have been the wrong call can be undone
        # without downloading anything again.
        write_parsed_document(
            directory=parsed_directory,
            fund_id=fund_id,
            source_id=source.source_id,
            document=document,
            suffix=PRIMARY_PARSE_SUFFIX,
        )

        document = decision.document

    parsed_path = write_parsed_document(
        directory=parsed_directory,
        fund_id=fund_id,
        source_id=source.source_id,
        document=document,
    )

    return (
        load_parsed_document(parsed_path),
        parsed_path,
        decision,
    )


# A page of a text PDF holds far more than this. A document averaging
# less is either empty or an image of a document.
RECOGNITION_CHARACTERS_PER_PAGE = 120


def _needs_recognition(
    document: ParsedDocument,
) -> bool:
    """Return whether a document holds too little text to be usable."""

    if document.document_format is not DocumentFormat.PDF:
        return False

    if not document.page_count:
        return False

    if document.scanned_candidate:
        return True

    return document.character_count < RECOGNITION_CHARACTERS_PER_PAGE * document.page_count


async def parse_fund_documents(
    *,
    database_path: Path,
    fund: FundInput,
    parsed_directory: Path,
    force: bool = False,
    allow_ocr: bool = False,
    allow_anydoc_fallback: bool = False,
) -> DocumentParsingSummary:
    """
    Parse all downloaded sources belonging to one fund.

    ``allow_ocr`` permits optical recognition for the documents that
    produced no usable text. It is off by default, because recognising
    every PDF would cost far more than it returns.

    ``allow_anydoc_fallback`` permits a second reading of the PDFs whose
    layout defeated the parser. The primary parser still reads every
    document first and keeps the result unless the second reading is
    measurably better.
    """

    fund_id = stable_fund_id(fund)

    sources = list_parseable_sources(
        database_path,
        fund_id=fund_id,
        include_parsed=force,
    )

    failures: list[DocumentParsingFailure] = []

    all_facts: list[DocumentFacts] = []

    documents_parsed = 0
    scanned_candidates = 0
    total_characters = 0

    fallback_decisions: list[FallbackDecision] = []

    for source in sources:
        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="parse_document",
            status=AttemptStatus.STARTED,
            url=source.url,
        )

        try:
            if source.local_path is None:
                raise DocumentParseError("Downloaded source does not contain a local file path")

            validated_document, parsed_path, fallback_decision = await asyncio.to_thread(
                _parse_source_document,
                source=source,
                parsed_directory=parsed_directory,
                fund_id=fund_id,
                fund_name=fund.name,
                allow_ocr=allow_ocr,
                allow_anydoc_fallback=allow_anydoc_fallback,
            )

            fallback_decisions.append(fallback_decision)

            record_parsed_document(
                database_path,
                source_id=source.source_id,
                fund_id=fund_id,
                document_format=(validated_document.document_format.value),
                parser_name=(validated_document.parser_name),
                page_count=(validated_document.page_count),
                character_count=(validated_document.character_count),
                scanned_candidate=(validated_document.scanned_candidate),
                text_path=str(parsed_path),
            )
        except (
            OSError,
            DatabaseError,
            DocumentParseError,
        ) as exc:
            error_code = (
                exc.code
                if isinstance(
                    exc,
                    DocumentParseError,
                )
                else "document_processing_error"
            )

            failures.append(
                DocumentParsingFailure(
                    source_id=source.source_id,
                    url=source.url,
                    error_code=error_code,
                    message=str(exc),
                )
            )

            record_attempt(
                database_path,
                fund_id=fund_id,
                stage="parse_document",
                status=AttemptStatus.FAILED,
                url=source.url,
                error_code=error_code,
                error_message=str(exc),
            )

            continue

        documents_parsed += 1
        total_characters += validated_document.character_count

        if validated_document.scanned_candidate:
            scanned_candidates += 1

        facts = describe_document(
            record=ParsedDocumentRecord(
                source_id=source.source_id,
                fund_id=fund_id,
                url=source.url,
                title=None,
                document_type=source.document_type,
                content_type=source.content_type,
                retrieved_at=None,
                document_format=(validated_document.document_format.value),
                parser_name=(validated_document.parser_name),
                page_count=(validated_document.page_count),
                character_count=(validated_document.character_count),
                scanned_candidate=(validated_document.scanned_candidate),
                text_path=str(parsed_path),
                parsed_at="",
                local_path=source.local_path,
            ),
            document=validated_document,
            fund_name=fund.name,
        )

        all_facts.append(facts)

        storage_failure = store_document_facts_quietly(
            database_path=database_path,
            fund_id=fund_id,
            facts=facts,
        )

        if storage_failure is not None:
            failures.append(
                DocumentParsingFailure(
                    source_id=source.source_id,
                    url=source.url,
                    error_code="document_metadata_error",
                    message=storage_failure,
                )
            )

        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="parse_document",
            status=AttemptStatus.SUCCEEDED,
            url=source.url,
        )

    return DocumentParsingSummary(
        fund_id=fund_id,
        fund_name=fund.name,
        sources_considered=len(sources),
        documents_parsed=documents_parsed,
        scanned_candidates=scanned_candidates,
        total_characters=total_characters,
        failures=tuple(failures),
        documents_classified=sum(
            1 for item in all_facts if item.classification.document_type.value != "other"
        ),
        documents_with_published_date=sum(
            1 for item in all_facts if item.dates.published_at is not None
        ),
        documents_with_effective_date=sum(
            1 for item in all_facts if item.dates.effective_at is not None
        ),
        scope_counts=_counted(item.identity.scope.value for item in all_facts),
        document_type_counts=_counted(
            item.classification.document_type.value for item in all_facts
        ),
        parser_counts=_counted(item.parser_name for item in all_facts),
        ocr_candidates=sum(
            1
            for item in all_facts
            if not item.ocr_used
            and any("optical recognition" in warning for warning in item.warnings)
        ),
        ocr_used=sum(1 for item in all_facts if item.ocr_used),
        ocr_available=ocr_available(),
        anydoc_triggered=sum(
            1
            for decision in fallback_decisions
            if decision.outcome is not FallbackOutcome.NOT_TRIGGERED
        ),
        anydoc_accepted=sum(1 for decision in fallback_decisions if decision.accepted),
        anydoc_rejected=sum(
            1 for decision in fallback_decisions if decision.outcome is FallbackOutcome.REJECTED
        ),
        anydoc_blocked=sum(
            1 for decision in fallback_decisions if decision.outcome is FallbackOutcome.BLOCKED
        ),
        anydoc_failed=sum(
            1 for decision in fallback_decisions if decision.outcome is FallbackOutcome.FAILED
        ),
        anydoc_seconds=sum(decision.conversion_seconds for decision in fallback_decisions),
    )


def _counted(
    values: Iterable[str],
) -> tuple[tuple[str, int], ...]:
    """Return how often each value occurred, in a stable order."""

    counted = Counter(values)

    return tuple(sorted(counted.items()))
