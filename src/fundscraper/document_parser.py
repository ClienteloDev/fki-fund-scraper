from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self
from urllib.parse import urlsplit

import pymupdf
from selectolax.lexbor import LexborHTMLParser


class DocumentFormat(StrEnum):
    PDF = "pdf"
    HTML = "html"
    XHTML = "xhtml"
    XML = "xml"
    TEXT = "text"


class DocumentParseError(RuntimeError):
    """Base error raised while converting a document to text."""

    code = "document_parse_error"


class UnsupportedDocumentError(DocumentParseError):
    code = "unsupported_document_format"


class CorruptedDocumentError(DocumentParseError):
    code = "corrupted_document"


class PasswordProtectedDocumentError(DocumentParseError):
    code = "password_protected_document"


class ParsedDocumentStorageError(DocumentParseError):
    code = "parsed_document_storage_error"


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page_number: int | None
    text: str
    character_count: int

    def to_json_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "text": self.text,
            "character_count": self.character_count,
        }

    @classmethod
    def from_json_dict(
        cls,
        payload: dict[str, Any],
    ) -> Self:
        page_number_value = payload.get("page_number")

        page_number = int(page_number_value) if page_number_value is not None else None

        text = str(payload["text"])

        character_count = int(payload["character_count"])

        return cls(
            page_number=page_number,
            text=text,
            character_count=character_count,
        )


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    document_format: DocumentFormat
    parser_name: str
    pages: tuple[ParsedPage, ...]
    character_count: int
    scanned_candidate: bool

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "document_format": self.document_format.value,
            "parser_name": self.parser_name,
            "page_count": self.page_count,
            "character_count": self.character_count,
            "scanned_candidate": self.scanned_candidate,
            "pages": [page.to_json_dict() for page in self.pages],
        }

    @classmethod
    def from_json_dict(
        cls,
        payload: dict[str, Any],
    ) -> Self:
        raw_pages = payload.get("pages")

        if not isinstance(
            raw_pages,
            list,
        ):
            raise ValueError("Parsed document pages must be an array")

        pages = tuple(
            ParsedPage.from_json_dict(page_payload)
            for page_payload in raw_pages
            if isinstance(
                page_payload,
                dict,
            )
        )

        declared_page_count = int(payload["page_count"])

        if declared_page_count != len(pages):
            raise ValueError("Parsed document page count does not match pages")

        character_count = int(payload["character_count"])

        calculated_character_count = sum(page.character_count for page in pages)

        if character_count != calculated_character_count:
            raise ValueError("Parsed document character count does not match pages")

        return cls(
            document_format=DocumentFormat(str(payload["document_format"])),
            parser_name=str(payload["parser_name"]),
            pages=pages,
            character_count=character_count,
            scanned_candidate=bool(payload["scanned_candidate"]),
        )


def detect_document_format(
    *,
    body: bytes,
    content_type: str | None,
    url: str,
) -> DocumentFormat:
    """Detect the supported document format from bytes, type and URL."""

    body_prefix = body[:2048].lstrip().lower()

    normalized_content_type = (
        content_type.split(
            ";",
            maxsplit=1,
        )[0]
        .strip()
        .lower()
        if content_type
        else ""
    )

    suffix = PurePosixPath(urlsplit(url).path).suffix.casefold()

    if body_prefix.startswith(b"%pdf-"):
        return DocumentFormat.PDF

    if normalized_content_type == "application/pdf":
        return DocumentFormat.PDF

    if suffix == ".pdf":
        return DocumentFormat.PDF

    if normalized_content_type == "application/xhtml+xml":
        return DocumentFormat.XHTML

    if suffix == ".xhtml":
        return DocumentFormat.XHTML

    if normalized_content_type == "text/html":
        return DocumentFormat.HTML

    if suffix in {
        ".html",
        ".htm",
    }:
        return DocumentFormat.HTML

    if normalized_content_type in {
        "application/xml",
        "text/xml",
    }:
        return DocumentFormat.XML

    if suffix == ".xml":
        return DocumentFormat.XML

    if normalized_content_type.startswith("text/plain"):
        return DocumentFormat.TEXT

    if (
        body_prefix.startswith(b"<!doctype html")
        or body_prefix.startswith(b"<html")
        or b"<html" in body_prefix
    ):
        return DocumentFormat.HTML

    if body_prefix.startswith(b"<?xml"):
        return DocumentFormat.XML

    raise UnsupportedDocumentError("Document format could not be determined")


def parse_document(
    *,
    body: bytes,
    content_type: str | None,
    url: str,
) -> ParsedDocument:
    """Convert one supported document into normalized page text."""

    if not body:
        raise CorruptedDocumentError("Document body is empty")

    document_format = detect_document_format(
        body=body,
        content_type=content_type,
        url=url,
    )

    if document_format is DocumentFormat.PDF:
        return parse_pdf_document(body)

    if document_format in {
        DocumentFormat.HTML,
        DocumentFormat.XHTML,
        DocumentFormat.XML,
    }:
        return parse_markup_document(
            body=body,
            document_format=document_format,
        )

    if document_format is DocumentFormat.TEXT:
        return parse_plain_text_document(body)

    raise UnsupportedDocumentError(f"Unsupported document format: {document_format}")


def parse_pdf_document(
    body: bytes,
) -> ParsedDocument:
    """Extract plain text from a PDF while preserving page numbers."""

    pages: list[ParsedPage] = []

    try:
        with pymupdf.open(  # type: ignore[no-untyped-call]
            stream=body,
            filetype="pdf",
        ) as document:
            if document.needs_pass:
                raise PasswordProtectedDocumentError("PDF requires a password")

            for page_index in range(document.page_count):
                page = document.load_page(page_index)

                raw_text = page.get_text(
                    "text",
                    sort=True,
                )

                normalized_text = normalize_extracted_text(raw_text)

                pages.append(
                    ParsedPage(
                        page_number=(page_index + 1),
                        text=normalized_text,
                        character_count=len(normalized_text),
                    )
                )
    except PasswordProtectedDocumentError:
        raise
    except (
        RuntimeError,
        ValueError,
    ) as exc:
        raise CorruptedDocumentError(f"PDF could not be parsed: {exc}") from exc

    total_characters = sum(page.character_count for page in pages)

    pages_with_text = sum(1 for page in pages if page.character_count >= 20)

    scanned_candidate = _is_scanned_pdf_candidate(
        page_count=len(pages),
        pages_with_text=pages_with_text,
        character_count=total_characters,
    )

    return ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=tuple(pages),
        character_count=total_characters,
        scanned_candidate=scanned_candidate,
    )


def parse_markup_document(
    *,
    body: bytes,
    document_format: DocumentFormat,
) -> ParsedDocument:
    """Extract visible text from HTML, XHTML or XML."""

    try:
        parser = LexborHTMLParser(body)

        parser.strip_tags(
            [
                "script",
                "style",
                "noscript",
                "template",
                "svg",
                "canvas",
                "iframe",
            ]
        )

        body_node = parser.body

        if body_node is not None:
            raw_text = body_node.text(
                separator="\n",
                strip=True,
                skip_empty=True,
            )
        else:
            raw_text = parser.text(
                separator="\n",
                strip=True,
                skip_empty=True,
            )
    except (
        RuntimeError,
        ValueError,
    ) as exc:
        raise CorruptedDocumentError(f"Markup document could not be parsed: {exc}") from exc

    normalized_text = normalize_extracted_text(raw_text)

    page = ParsedPage(
        page_number=None,
        text=normalized_text,
        character_count=len(normalized_text),
    )

    return ParsedDocument(
        document_format=document_format,
        parser_name="selectolax",
        pages=(page,),
        character_count=page.character_count,
        scanned_candidate=False,
    )


def parse_plain_text_document(
    body: bytes,
) -> ParsedDocument:
    """Decode a plain text source."""

    decoded_text = body.decode(
        "utf-8-sig",
        errors="replace",
    )

    normalized_text = normalize_extracted_text(decoded_text)

    page = ParsedPage(
        page_number=None,
        text=normalized_text,
        character_count=len(normalized_text),
    )

    return ParsedDocument(
        document_format=DocumentFormat.TEXT,
        parser_name="plain_text",
        pages=(page,),
        character_count=page.character_count,
        scanned_candidate=False,
    )


def normalize_extracted_text(
    value: str,
) -> str:
    """Normalize whitespace without destroying document line structure."""

    normalized_value = value.replace(
        "\xa0",
        " ",
    ).replace(
        "\u00ad",
        "",
    )

    normalized_lines: list[str] = []

    previous_line_empty = False

    for raw_line in normalized_value.splitlines():
        line = re.sub(
            r"[ \t]+",
            " ",
            raw_line,
        ).strip()

        if not line:
            if normalized_lines and not previous_line_empty:
                normalized_lines.append("")

            previous_line_empty = True
            continue

        normalized_lines.append(line)

        previous_line_empty = False

    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()

    return "\n".join(normalized_lines)


def write_parsed_document(
    *,
    directory: Path,
    fund_id: str,
    source_id: int,
    document: ParsedDocument,
) -> Path:
    """Persist a parsed document as an atomic JSON file."""

    fund_directory = directory / fund_id

    fund_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = fund_directory / f"{source_id}.json"

    temporary_path = output_path.with_suffix(".json.tmp")

    try:
        temporary_path.write_text(
            json.dumps(
                document.to_json_dict(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(output_path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)

        raise ParsedDocumentStorageError(f"Parsed document could not be saved: {exc}") from exc

    return output_path


def load_parsed_document(
    path: Path,
) -> ParsedDocument:
    """Load and validate a parsed document JSON file."""

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ParsedDocumentStorageError(f"Parsed document does not exist: {path}") from exc
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise ParsedDocumentStorageError(
            f"Parsed document could not be loaded: {path}: {exc}"
        ) from exc

    if not isinstance(
        payload,
        dict,
    ):
        raise ParsedDocumentStorageError("Parsed document root must be an object")

    try:
        return ParsedDocument.from_json_dict(payload)
    except (
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ParsedDocumentStorageError(f"Parsed document is invalid: {path}: {exc}") from exc


def _is_scanned_pdf_candidate(
    *,
    page_count: int,
    pages_with_text: int,
    character_count: int,
) -> bool:
    """
    Detect PDFs that probably contain scanned images instead of text.

    A short document is not automatically considered scanned. The
    strongest signals are no extracted text or text on very few pages.
    """

    if page_count == 0:
        return False

    if character_count == 0:
        return True

    text_page_ratio = pages_with_text / page_count

    minimum_expected_characters = max(
        20,
        page_count * 20,
    )

    return character_count < minimum_expected_characters or text_page_ratio < 0.25
