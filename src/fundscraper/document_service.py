from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from fundscraper.database import (
    AttemptStatus,
    DatabaseError,
    SourceRecord,
    list_parseable_sources,
    record_attempt,
    record_parsed_document,
)
from fundscraper.document_parser import (
    DocumentParseError,
    ParsedDocument,
    load_parsed_document,
    parse_document,
    write_parsed_document,
)
from fundscraper.models import FundInput
from fundscraper.output_service import stable_fund_id


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


def _parse_source_document(
    *,
    source: SourceRecord,
    parsed_directory: Path,
    fund_id: str,
) -> tuple[ParsedDocument, Path]:
    """Read, parse and validate one source outside the event-loop thread."""

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

    parsed_path = write_parsed_document(
        directory=parsed_directory,
        fund_id=fund_id,
        source_id=source.source_id,
        document=document,
    )

    return (
        load_parsed_document(parsed_path),
        parsed_path,
    )


async def parse_fund_documents(
    *,
    database_path: Path,
    fund: FundInput,
    parsed_directory: Path,
    force: bool = False,
) -> DocumentParsingSummary:
    """Parse all downloaded sources belonging to one fund."""

    fund_id = stable_fund_id(fund)

    sources = list_parseable_sources(
        database_path,
        fund_id=fund_id,
        include_parsed=force,
    )

    failures: list[DocumentParsingFailure] = []

    documents_parsed = 0
    scanned_candidates = 0
    total_characters = 0

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

            validated_document, parsed_path = await asyncio.to_thread(
                _parse_source_document,
                source=source,
                parsed_directory=parsed_directory,
                fund_id=fund_id,
            )

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
    )
