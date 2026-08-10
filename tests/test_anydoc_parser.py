from __future__ import annotations

from typing import cast

import pymupdf
import pytest

from fundscraper.anydoc_parser import (
    ANYDOC_PARSER_NAME,
    AnyDocParseError,
    AnyDocUnsupportedFormatError,
    anydoc_available,
    anydoc_capabilities,
    anydoc_supports,
    extract_markdown_tables,
    parse_document_with_anydoc,
    parsed_document_from_markdown,
    strip_markdown_emphasis,
)
from fundscraper.document_parser import DocumentFormat

requires_anydoc = pytest.mark.skipif(
    not anydoc_available(),
    reason="the optional anydoc extra is not installed",
)


def create_text_pdf(
    text: str,
) -> bytes:
    document = pymupdf.open()  # type: ignore[no-untyped-call]

    try:
        page = document.new_page()

        page.insert_text(
            (
                72,
                72,
            ),
            text,
        )

        result = cast(
            bytes,
            document.tobytes(),  # type: ignore[no-untyped-call]
        )

        return result
    finally:
        document.close()  # type: ignore[no-untyped-call]


def test_reports_the_formats_it_can_read() -> None:
    assert anydoc_supports(DocumentFormat.PDF)

    # Half of the parsed cache is markup, and AnyDoc has no reader for it.
    assert not anydoc_supports(DocumentFormat.HTML)

    assert not anydoc_supports(DocumentFormat.XML)


def test_capabilities_describe_the_installation() -> None:
    capabilities = anydoc_capabilities()

    assert capabilities["available"] is anydoc_available()

    assert capabilities["supported_formats"] == ["pdf"]


def test_strips_emphasis_but_keeps_the_words() -> None:
    stripped = strip_markdown_emphasis("**Vstupní poplatek** je *max.* 4 %")

    assert stripped == "Vstupní poplatek je max. 4 %"


def test_keeps_an_asterisk_that_is_not_emphasis() -> None:
    assert strip_markdown_emphasis("Dopad ročních nákladů (*).") == "Dopad ročních nákladů (*)."


def test_reads_a_markdown_table_back_into_rows() -> None:
    markdown = "\n".join(
        [
            "| Poplatek | Sazba |",
            "|---|---|",
            "| **Vstupní poplatek** | max. 4 % |",
            "| Výstupní poplatek | max. 15 % |",
        ]
    )

    tables = extract_markdown_tables(markdown)

    assert len(tables) == 1

    assert tables[0].rows == (
        ("Poplatek", "Sazba"),
        ("Vstupní poplatek", "max. 4 %"),
        ("Výstupní poplatek", "max. 15 %"),
    )


def test_reads_a_row_without_a_closing_pipe() -> None:
    markdown = "| a | 1\n|---|---|\n| b | 2"

    tables = extract_markdown_tables(markdown)

    assert tables[0].rows == (
        ("a", "1"),
        ("b", "2"),
    )


def test_separates_two_tables_split_by_a_paragraph() -> None:
    markdown = "\n".join(
        [
            "| a | 1 |",
            "| b | 2 |",
            "",
            "Text mezi tabulkami.",
            "",
            "| c | 3 |",
            "| d | 4 |",
        ]
    )

    assert len(extract_markdown_tables(markdown)) == 2


def test_drops_a_finding_that_carries_no_relationship() -> None:
    # One column cannot tie a value to a label, and the PDF parser drops
    # the same shape, so the table counts of the two stay comparable.
    assert extract_markdown_tables("| jediny sloupec |\n| druhy radek |") == []

    assert extract_markdown_tables("| a | 1 |") == []


def test_wraps_markdown_as_one_page_of_unknown_number() -> None:
    document = parsed_document_from_markdown(
        markdown="# Statut\n\nText fondu.",
        document_format=DocumentFormat.PDF,
    )

    assert document.parser_name == ANYDOC_PARSER_NAME

    assert document.page_count == 1

    # AnyDoc reports no page boundaries, so no page number is invented.
    assert document.pages[0].page_number is None

    assert document.character_count == document.pages[0].character_count

    assert not document.scanned_candidate


def test_empty_conversion_is_reported_as_scanned() -> None:
    document = parsed_document_from_markdown(
        markdown="   \n\n  ",
        document_format=DocumentFormat.PDF,
    )

    assert document.scanned_candidate

    assert document.character_count == 0


def test_rejects_a_format_anydoc_cannot_read() -> None:
    with pytest.raises(AnyDocUnsupportedFormatError):
        parse_document_with_anydoc(
            body=b"<html><body>text</body></html>",
            document_format=DocumentFormat.HTML,
        )


@requires_anydoc
def test_rejects_an_empty_body() -> None:
    with pytest.raises(AnyDocParseError):
        parse_document_with_anydoc(
            body=b"",
            document_format=DocumentFormat.PDF,
        )


@requires_anydoc
def test_converts_a_real_pdf() -> None:
    document = parse_document_with_anydoc(
        body=create_text_pdf("Vstupni poplatek max 4 %"),
        document_format=DocumentFormat.PDF,
    )

    assert document.document_format is DocumentFormat.PDF

    assert document.parser_name == ANYDOC_PARSER_NAME

    assert "poplatek" in document.full_text


@requires_anydoc
def test_reports_a_pdf_it_refuses_as_a_parse_error() -> None:
    with pytest.raises(AnyDocParseError):
        parse_document_with_anydoc(
            body=b"%PDF-1.4 not really a pdf",
            document_format=DocumentFormat.PDF,
        )
