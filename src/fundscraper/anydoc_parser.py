"""
Experimental adapter around the AnyDoc document converter.

AnyDoc (https://firecrawl.github.io/anydoc/) is a Rust converter that turns
office documents and PDFs into GitHub-Flavored Markdown. It is wired in here
as an *alternative* to the Step 6 parser so the two can be measured against
each other; nothing in the pipeline calls it, and
:func:`fundscraper.document_parser.parse_document` is untouched.

The dependency is optional. Everything in this module answers honestly when
AnyDoc is not installed, so importing it can never break a normal run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any, Final

from fundscraper.document_parser import (
    MINIMUM_TABLE_COLUMNS,
    MINIMUM_TABLE_ROWS,
    DocumentFormat,
    DocumentParseError,
    DocumentTable,
    ParsedDocument,
    ParsedPage,
    normalize_extracted_text,
)

ANYDOC_PARSER_NAME: Final = "anydoc"


# The formats this project actually meets that AnyDoc can convert. AnyDoc
# handles the office family and PDF; it has no HTML or XML reader at all,
# which matters because roughly half of the parsed cache is markup.
ANYDOC_SUPPORTED_FORMATS: Final[frozenset[DocumentFormat]] = frozenset({DocumentFormat.PDF})


class AnyDocUnavailableError(DocumentParseError):
    """Raised when AnyDoc is asked for but is not installed here."""

    code = "anydoc_unavailable"


class AnyDocUnsupportedFormatError(DocumentParseError):
    """Raised for a format AnyDoc has no reader for, such as HTML."""

    code = "anydoc_unsupported_format"


class AnyDocParseError(DocumentParseError):
    """Raised when AnyDoc itself refused to convert the document."""

    code = "anydoc_parse_error"


def _anydoc_module() -> ModuleType | None:
    """Import AnyDoc once, or report that it is not installed."""

    try:
        return import_module("anydoc")
    except ImportError:
        return None


def anydoc_available() -> bool:
    """Return whether the optional AnyDoc dependency can be used here."""

    return _anydoc_module() is not None


def anydoc_version() -> str | None:
    """Return the installed AnyDoc version, or nothing when absent."""

    module = _anydoc_module()

    if module is None:
        return None

    version = getattr(module, "__version__", None)

    if isinstance(version, str):
        return version

    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as distribution_version

    try:
        return distribution_version("firecrawl-anydoc")
    except PackageNotFoundError:
        return None


def anydoc_supports(document_format: DocumentFormat) -> bool:
    """Return whether AnyDoc has a reader for this document format."""

    return document_format in ANYDOC_SUPPORTED_FORMATS


def is_anydoc_parser(
    parser_name: str,
) -> bool:
    """
    Return whether a parse came from AnyDoc.

    The name may carry the reason it was used, the way the PDF parser
    names the rung it answered from, so only the leading word is compared.
    """

    return parser_name.split("+", maxsplit=1)[0] == ANYDOC_PARSER_NAME


def convert_to_markdown(
    *,
    body: bytes,
    document_format: DocumentFormat,
) -> str:
    """Run AnyDoc over one document and return its Markdown."""

    module = _anydoc_module()

    if module is None:
        raise AnyDocUnavailableError(
            "AnyDoc is not installed; install the optional 'anydoc' extra to use it"
        )

    if not anydoc_supports(document_format):
        raise AnyDocUnsupportedFormatError(f"AnyDoc has no reader for format: {document_format}")

    if not body:
        raise AnyDocParseError("Document body is empty")

    try:
        return str(
            module.to_markdown_bytes(
                body,
                document_format.value,
            )
        )
    except Exception as exc:  # noqa: BLE001 - AnyDoc raises its own error tree
        raise AnyDocParseError(f"AnyDoc could not convert the document: {exc}") from exc


def parse_document_with_anydoc(
    *,
    body: bytes,
    document_format: DocumentFormat,
) -> ParsedDocument:
    """
    Convert one document to a :class:`ParsedDocument` through AnyDoc.

    AnyDoc returns a single Markdown string with no page boundaries in it,
    so the result is one page whose number is unknown. That is the same
    shape the markup parser already produces, and it is reported honestly
    rather than guessed at, because a page number invented here would end
    up in the evidence of an extracted value.

    :func:`fundscraper.anydoc_fallback.attribute_pages` recovers the page
    numbers afterwards by matching the conversion back onto the pages the
    primary parser already read.
    """

    return parsed_document_from_markdown(
        markdown=convert_to_markdown(
            body=body,
            document_format=document_format,
        ),
        document_format=document_format,
    )


def parsed_document_from_markdown(
    *,
    markdown: str,
    document_format: DocumentFormat,
) -> ParsedDocument:
    """Wrap one Markdown conversion in the parsed-document shape."""

    tables = tuple(extract_markdown_tables(markdown))

    text = normalize_extracted_text(strip_markdown_emphasis(markdown))

    page = ParsedPage(
        page_number=None,
        text=text,
        character_count=len(text),
        tables=tables,
    )

    return ParsedDocument(
        document_format=document_format,
        parser_name=ANYDOC_PARSER_NAME,
        pages=(page,),
        character_count=page.character_count,
        scanned_candidate=len(text.strip()) == 0,
    )


# AnyDoc turns every run of bold or italic glyphs into emphasis markers. On
# a PDF whose labels are set in a heavier face that produces "**Poplatek**
# **2 %**", which breaks any phrase a value is looked up by. The words are
# what carries meaning here, so the markers are dropped and the text left
# in place.
_BOLD_MARKER_PATTERN: Final = re.compile(r"\*\*|__")

_ITALIC_MARKER_PATTERN: Final = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")


def strip_markdown_emphasis(
    markdown: str,
) -> str:
    """Remove emphasis markers while keeping the words and the layout."""

    without_bold = _BOLD_MARKER_PATTERN.sub(
        "",
        markdown,
    )

    return _ITALIC_MARKER_PATTERN.sub(
        r"\1",
        without_bold,
    )


_TABLE_DELIMITER_PATTERN: Final = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _table_row_cells(
    line: str,
) -> tuple[str, ...] | None:
    """Split one Markdown table line into its cells, or reject the line."""

    stripped = line.strip()

    if not stripped.startswith("|"):
        return None

    # A trailing pipe closes the last cell; without it the row is still
    # valid GitHub-Flavored Markdown.
    inner = stripped[1:]

    if inner.endswith("|"):
        inner = inner[:-1]

    return tuple(strip_markdown_emphasis(cell).strip() for cell in inner.split("|"))


@dataclass(frozen=True, slots=True)
class MarkdownChunk:
    """
    One passage of a conversion, in the order AnyDoc wrote it.

    Chunks exist so the conversion can be matched back onto the pages the
    primary parser read. A paragraph and a table are each small enough to
    belong to one page and large enough to be recognised on it.
    """

    text: str
    table: DocumentTable | None = None

    @property
    def is_table(self) -> bool:
        return self.table is not None


def _table_from_rows(
    rows: tuple[tuple[str, ...], ...],
) -> DocumentTable | None:
    """Build a table from Markdown rows, or reject its shape."""

    kept = tuple(row for row in rows if any(cell for cell in row))

    if len(kept) < MINIMUM_TABLE_ROWS:
        return None

    if max((len(row) for row in kept), default=0) < MINIMUM_TABLE_COLUMNS:
        return None

    return DocumentTable(rows=kept)


def markdown_chunks(
    markdown: str,
) -> list[MarkdownChunk]:
    """
    Split one conversion into the passages it is made of.

    A run of table lines becomes one chunk carrying its rows; everything
    between blank lines becomes a chunk of text. A run of table lines
    whose shape carries no relationship still becomes a chunk, so its
    words are not lost from the text, but it holds no table.
    """

    chunks: list[MarkdownChunk] = []

    table_lines: list[str] = []

    table_rows: list[tuple[str, ...]] = []

    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return

        text = strip_markdown_emphasis("\n".join(paragraph_lines)).strip()

        paragraph_lines.clear()

        if text:
            chunks.append(MarkdownChunk(text=text))

    def flush_table() -> None:
        if not table_lines:
            return

        text = strip_markdown_emphasis("\n".join(table_lines)).strip()

        table = _table_from_rows(tuple(table_rows))

        table_lines.clear()

        table_rows.clear()

        if text:
            chunks.append(
                MarkdownChunk(
                    text=text,
                    table=table,
                )
            )

    for line in markdown.splitlines():
        cells = _table_row_cells(line)

        if cells is not None:
            flush_paragraph()

            table_lines.append(line)

            if not _TABLE_DELIMITER_PATTERN.match(line.strip()):
                # The header underline carries alignment, not content.
                table_rows.append(cells)

            continue

        flush_table()

        if line.strip():
            paragraph_lines.append(line)
        else:
            flush_paragraph()

    flush_table()

    flush_paragraph()

    return chunks


def extract_markdown_tables(
    markdown: str,
) -> list[DocumentTable]:
    """
    Read every GitHub-Flavored Markdown table back into rows of cells.

    The rows are what ties a rate to the holding period it belongs to, so
    they are recovered rather than left inside the text. The same shape
    filter the PDF parser applies is used, so a one-column or one-row
    finding is dropped from both parsers alike and the table counts stay
    comparable.
    """

    return [chunk.table for chunk in markdown_chunks(markdown) if chunk.table is not None]


def anydoc_capabilities() -> dict[str, Any]:
    """Describe the installed AnyDoc for a report header."""

    return {
        "available": anydoc_available(),
        "version": anydoc_version(),
        "supported_formats": sorted(fmt.value for fmt in ANYDOC_SUPPORTED_FORMATS),
    }
