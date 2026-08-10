from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
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
class TextBlock:
    """One block of text with the place on the page it was drawn at."""

    order: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "order": self.order,
            "bbox": [
                round(self.x0, 1),
                round(self.y0, 1),
                round(self.x1, 1),
                round(self.y1, 1),
            ],
            "text": self.text,
        }

    @classmethod
    def from_json_dict(
        cls,
        payload: dict[str, Any],
    ) -> Self:
        bbox = payload.get("bbox") or [0, 0, 0, 0]

        return cls(
            order=int(payload.get("order", 0)),
            x0=float(bbox[0]),
            y0=float(bbox[1]),
            x1=float(bbox[2]),
            y1=float(bbox[3]),
            text=str(payload.get("text", "")),
        )


@dataclass(frozen=True, slots=True)
class DocumentTable:
    """
    One table, kept as rows of cells rather than as flattened text.

    A fee table read as a paragraph loses which rate belongs to which
    holding period. Keeping the rows preserves that relationship, which
    is the only reliable way to tie a value to its own label.
    """

    rows: tuple[tuple[str, ...], ...]
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "bbox": [
                round(self.x0, 1),
                round(self.y0, 1),
                round(self.x1, 1),
                round(self.y1, 1),
            ],
            "rows": [list(row) for row in self.rows],
        }

    @classmethod
    def from_json_dict(
        cls,
        payload: dict[str, Any],
    ) -> Self:
        bbox = payload.get("bbox") or [0, 0, 0, 0]

        raw_rows = payload.get("rows") or []

        return cls(
            rows=tuple(
                tuple(str(cell) for cell in row) for row in raw_rows if isinstance(row, list)
            ),
            x0=float(bbox[0]),
            y0=float(bbox[1]),
            x1=float(bbox[2]),
            y1=float(bbox[3]),
        )


@dataclass(frozen=True, slots=True)
class ParsedPage:
    page_number: int | None
    text: str
    character_count: int

    # The structure behind the text. Both default to empty so that a
    # document parsed before this existed still loads unchanged.
    blocks: tuple[TextBlock, ...] = ()
    tables: tuple[DocumentTable, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "page_number": self.page_number,
            "text": self.text,
            "character_count": self.character_count,
        }

        if self.blocks:
            payload["blocks"] = [block.to_json_dict() for block in self.blocks]

        if self.tables:
            payload["tables"] = [table.to_json_dict() for table in self.tables]

        return payload

    @classmethod
    def from_json_dict(
        cls,
        payload: dict[str, Any],
    ) -> Self:
        page_number_value = payload.get("page_number")

        page_number = int(page_number_value) if page_number_value is not None else None

        text = str(payload["text"])

        character_count = int(payload["character_count"])

        raw_blocks = payload.get("blocks") or []

        raw_tables = payload.get("tables") or []

        return cls(
            page_number=page_number,
            text=text,
            character_count=character_count,
            blocks=tuple(
                TextBlock.from_json_dict(item) for item in raw_blocks if isinstance(item, dict)
            ),
            tables=tuple(
                DocumentTable.from_json_dict(item) for item in raw_tables if isinstance(item, dict)
            ),
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

    @property
    def table_count(self) -> int:
        return sum(len(page.tables) for page in self.pages)

    def iter_tables(self) -> Iterator[tuple[int | None, DocumentTable]]:
        """Yield every table with the page number it was found on."""

        for page in self.pages:
            for table in page.tables:
                yield (
                    page.page_number,
                    table,
                )

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
    allow_ocr: bool = False,
) -> ParsedDocument:
    """
    Convert one supported document into normalized page text.

    ``allow_ocr`` only permits optical recognition; it does not force it.
    A PDF that yields text is never sent to OCR.
    """

    if not body:
        raise CorruptedDocumentError("Document body is empty")

    document_format = detect_document_format(
        body=body,
        content_type=content_type,
        url=url,
    )

    if document_format is DocumentFormat.PDF:
        return parse_pdf_document(
            body,
            allow_ocr=allow_ocr,
        )

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
    *,
    allow_ocr: bool = False,
) -> ParsedDocument:
    """
    Extract text from a PDF, page by page, through the parser ladder.

    Every page is tried with the cheapest strategy first and only falls
    through to a more expensive one when the previous produced almost
    nothing. Optical recognition sits at the very end and runs only when
    it is asked for, available, and the page holds no text at all.
    """

    pages: list[ParsedPage] = []

    strategies: set[str] = set()

    try:
        with pymupdf.open(  # type: ignore[no-untyped-call]
            stream=body,
            filetype="pdf",
        ) as document:
            if document.needs_pass:
                raise PasswordProtectedDocumentError("PDF requires a password")

            for page_index in range(document.page_count):
                page = document.load_page(page_index)

                page_blocks = _page_blocks(page)

                page_tables = _page_tables(page)

                raw_text, used = _page_text_by_ladder(
                    page=page,
                    blocks=page_blocks,
                    tables=page_tables,
                    allow_ocr=allow_ocr,
                )

                strategies.update(used)

                normalized_text = normalize_extracted_text(raw_text)

                pages.append(
                    ParsedPage(
                        page_number=(page_index + 1),
                        text=normalized_text,
                        character_count=len(normalized_text),
                        blocks=page_blocks,
                        tables=page_tables,
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
        parser_name=_parser_name(strategies),
        pages=tuple(pages),
        character_count=total_characters,
        scanned_candidate=scanned_candidate,
    )


# Below this many characters a page is treated as having defeated the
# strategy that produced it, and the next one down the ladder is tried.
ALTERNATE_STRATEGY_THRESHOLD = 40


# The order strategies are reported in, cheapest first, so that one
# document always names them the same way.
STRATEGY_ORDER = (
    "blocks",
    "tables",
    "ocr",
)


def _parser_name(
    strategies: set[str],
) -> str:
    """Return the parser label naming every strategy that contributed."""

    used = [name for name in STRATEGY_ORDER if name in strategies]

    return "+".join(("pymupdf", *used))


def _page_text_by_ladder(
    *,
    page: object,
    blocks: tuple[TextBlock, ...],
    tables: tuple[DocumentTable, ...],
    allow_ocr: bool,
) -> tuple[str, set[str]]:
    """
    Read one page, falling through the strategies until one succeeds.

    The ladder is ordered by cost. Reading order is free, rebuilding from
    blocks is cheap, rebuilding from table rows is cheap but only helps a
    page that is a table, and optical recognition is expensive enough
    that it must never run on a page that already gave its text.
    """

    used: set[str] = set()

    try:
        text = page.get_text(  # type: ignore[attr-defined]
            "text",
            sort=True,
        )
    except (RuntimeError, ValueError):
        text = ""

    if len(text.strip()) >= ALTERNATE_STRATEGY_THRESHOLD:
        return (
            text,
            used,
        )

    # A multi-column or heavily positioned page defeats the reading-order
    # extractor. The blocks were laid out on the page, so rebuilding from
    # their positions recovers the words in the order they were drawn.
    block_text = "\n".join(block.text for block in blocks)

    if len(block_text.strip()) > len(text.strip()):
        text = block_text

        used.add("blocks")

    if len(text.strip()) >= ALTERNATE_STRATEGY_THRESHOLD:
        return (
            text,
            used,
        )

    # A page that is nothing but a table can leave the text extractors
    # empty while the cells themselves hold the content.
    table_text = "\n".join(
        "\t".join(cell for cell in row if cell) for table in tables for row in table.rows
    )

    if len(table_text.strip()) > len(text.strip()):
        text = table_text

        used.add("tables")

    if len(text.strip()) >= ALTERNATE_STRATEGY_THRESHOLD or not allow_ocr:
        return (
            text,
            used,
        )

    # Nothing readable is left. The page is an image, and only optical
    # recognition can say what is on it.
    recognised = _ocr_text(page)

    if len(recognised.strip()) > len(text.strip()):
        text = recognised

        used.add("ocr")

    return (
        text,
        used,
    )


def ocr_available() -> bool:
    """
    Return whether optical recognition can actually run here.

    PyMuPDF delegates recognition to Tesseract. Without the binary and
    its language data the call raises, so the caller is told in advance
    rather than through an exception per page.
    """

    if shutil.which("tesseract") is not None:
        return True

    return bool(os.environ.get("TESSDATA_PREFIX"))


def _ocr_text(
    page: object,
) -> str:
    """Recognise the text of one page image, or return nothing."""

    if not ocr_available():
        return ""

    try:
        textpage = page.get_textpage_ocr(  # type: ignore[attr-defined]
            flags=0,
            full=True,
        )

        return str(page.get_text("text", textpage=textpage))  # type: ignore[attr-defined]
    except (RuntimeError, ValueError, TypeError, OSError):
        return ""


# How many blocks of one page are kept. A dense report page holds a few
# dozen; a pathological one holds thousands, and storing them all would
# bloat the parsed cache without adding structure.
MAXIMUM_BLOCKS_PER_PAGE = 200


# A table needs at least this many rows and columns to carry a
# relationship. A single cell "table" is a text frame that the detector
# happened to draw a box around.
MINIMUM_TABLE_ROWS = 2

MINIMUM_TABLE_COLUMNS = 2


def _page_blocks(
    page: object,
) -> tuple[TextBlock, ...]:
    """Return the text blocks of one page, in the order they were drawn."""

    try:
        raw_blocks = page.get_text("blocks")  # type: ignore[attr-defined]
    except (RuntimeError, ValueError):
        return ()

    ordered = sorted(
        (block for block in raw_blocks if len(block) >= 5 and isinstance(block[4], str)),
        key=lambda block: (
            round(float(block[1]), 1),
            round(float(block[0]), 1),
        ),
    )

    blocks: list[TextBlock] = []

    for order, block in enumerate(ordered[:MAXIMUM_BLOCKS_PER_PAGE]):
        text = normalize_extracted_text(str(block[4]))

        if not text:
            continue

        blocks.append(
            TextBlock(
                order=order,
                x0=float(block[0]),
                y0=float(block[1]),
                x1=float(block[2]),
                y1=float(block[3]),
                text=text,
            )
        )

    return tuple(blocks)


def _page_tables(
    page: object,
) -> tuple[DocumentTable, ...]:
    """
    Return the tables of one page as rows of cells.

    The detector marks any boxed area as a table, so single-cell and
    single-column findings are dropped: they carry no row relationship
    and would only add noise to the parsed cache.
    """

    try:
        finder = page.find_tables()  # type: ignore[attr-defined]
    except (RuntimeError, ValueError, TypeError):
        return ()

    tables: list[DocumentTable] = []

    for found in getattr(finder, "tables", ()):
        try:
            extracted = found.extract()
        except (RuntimeError, ValueError):
            continue

        rows = tuple(
            tuple("" if cell is None else " ".join(str(cell).split()) for cell in row)
            for row in extracted
            if row
        )

        rows = tuple(row for row in rows if any(cell for cell in row))

        if len(rows) < MINIMUM_TABLE_ROWS:
            continue

        if max((len(row) for row in rows), default=0) < MINIMUM_TABLE_COLUMNS:
            continue

        bbox = getattr(found, "bbox", (0.0, 0.0, 0.0, 0.0))

        tables.append(
            DocumentTable(
                rows=rows,
                x0=float(bbox[0]),
                y0=float(bbox[1]),
                x1=float(bbox[2]),
                y1=float(bbox[3]),
            )
        )

    return tuple(tables)


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
