from __future__ import annotations

import pymupdf

from fundscraper.document_parser import (
    DocumentFormat,
    DocumentTable,
    ParsedDocument,
    ParsedPage,
    TextBlock,
    ocr_available,
    parse_document,
)
from fundscraper.document_service import _needs_recognition


def _pdf_with_text(
    lines: list[tuple[float, float, str]],
) -> bytes:
    """Build a one page PDF with text drawn at the given positions."""

    document = pymupdf.open()  # type: ignore[no-untyped-call]

    page = document.new_page()

    for x, y, text in lines:
        page.insert_text(
            (x, y),
            text,
            fontsize=11,
        )

    return bytes(document.tobytes())  # type: ignore[no-untyped-call]


def _image_only_pdf() -> bytes:
    """Build a one page PDF that carries no text at all."""

    document = pymupdf.open()  # type: ignore[no-untyped-call]

    page = document.new_page()

    page.draw_rect(
        pymupdf.Rect(50, 50, 200, 200),  # type: ignore[no-untyped-call]
        fill=(0, 0, 0),
    )

    return bytes(document.tobytes())  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------------------
# Structure is preserved rather than flattened
# ---------------------------------------------------------------------------


def test_a_parsed_page_keeps_its_blocks_with_their_positions() -> None:
    body = _pdf_with_text(
        [
            (72, 100, "STATUT FONDU"),
            (72, 140, "Rezidento Alfa SICAV, a.s."),
        ]
    )

    document = parse_document(
        body=body,
        content_type="application/pdf",
        url="https://x.cz/statut.pdf",
    )

    page = document.pages[0]

    assert page.blocks

    first = page.blocks[0]

    assert first.text

    assert first.x1 > first.x0

    assert first.y1 > first.y0

    # The blocks are ordered down the page, so a reader can rebuild it.
    assert [block.order for block in page.blocks] == list(range(len(page.blocks)))


def test_a_two_column_page_is_recovered_from_its_blocks() -> None:
    """Reading order fails on columns; the block positions do not."""

    body = _pdf_with_text(
        [
            (60, 100, "Vstupni poplatek"),
            (320, 100, "3 %"),
            (60, 130, "Vystupni poplatek"),
            (320, 130, "0 %"),
        ]
    )

    document = parse_document(
        body=body,
        content_type="application/pdf",
        url="https://x.cz/cenik.pdf",
    )

    text = document.full_text

    assert "Vstupni poplatek" in text

    assert "3 %" in text

    assert "Vystupni poplatek" in text


def test_the_structure_survives_a_round_trip_through_json() -> None:
    page = ParsedPage(
        page_number=1,
        text="Vystupni poplatek 10 %",
        character_count=22,
        blocks=(
            TextBlock(
                order=0,
                x0=1.0,
                y0=2.0,
                x1=3.0,
                y1=4.0,
                text="Vystupni poplatek",
            ),
        ),
        tables=(
            DocumentTable(
                rows=(
                    (
                        "do 1 roku",
                        "10 %",
                    ),
                    (
                        "po 2 letech",
                        "0 %",
                    ),
                ),
            ),
        ),
    )

    document = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(page,),
        character_count=page.character_count,
        scanned_candidate=False,
    )

    restored = ParsedDocument.from_json_dict(document.to_json_dict())

    assert restored.pages[0].blocks[0].text == "Vystupni poplatek"

    assert restored.pages[0].tables[0].rows == (
        (
            "do 1 roku",
            "10 %",
        ),
        (
            "po 2 letech",
            "0 %",
        ),
    )

    assert restored.table_count == 1

    assert list(restored.iter_tables())[0][0] == 1


def test_a_document_parsed_before_the_structure_existed_still_loads() -> None:
    """The parsed cache holds thousands of files without these fields."""

    payload = {
        "document_format": "pdf",
        "parser_name": "pymupdf",
        "page_count": 1,
        "character_count": 5,
        "scanned_candidate": False,
        "pages": [
            {
                "page_number": 1,
                "text": "hello",
                "character_count": 5,
            }
        ],
    }

    restored = ParsedDocument.from_json_dict(payload)

    assert restored.pages[0].blocks == ()

    assert restored.pages[0].tables == ()

    assert restored.table_count == 0


# ---------------------------------------------------------------------------
# The parser ladder
# ---------------------------------------------------------------------------


def test_a_readable_pdf_never_leaves_the_first_strategy() -> None:
    body = _pdf_with_text([(72, 100, "Statut fondu Rezidento Alfa SICAV, a.s. ucinny od 1.1.2026")])

    document = parse_document(
        body=body,
        content_type="application/pdf",
        url="https://x.cz/statut.pdf",
    )

    assert document.parser_name == "pymupdf"


def test_optical_recognition_is_never_used_unless_it_is_allowed() -> None:
    body = _image_only_pdf()

    document = parse_document(
        body=body,
        content_type="application/pdf",
        url="https://x.cz/sken.pdf",
    )

    assert "ocr" not in document.parser_name

    assert document.scanned_candidate


def test_a_page_with_text_is_not_sent_to_recognition_even_when_allowed() -> None:
    body = _pdf_with_text([(72, 100, "Vyrocni zprava za rok 2024 Rezidento Alfa SICAV")])

    document = parse_document(
        body=body,
        content_type="application/pdf",
        url="https://x.cz/vz.pdf",
        allow_ocr=True,
    )

    assert "ocr" not in document.parser_name


# ---------------------------------------------------------------------------
# Recognition eligibility
# ---------------------------------------------------------------------------


def test_only_a_document_without_usable_text_is_a_recognition_candidate() -> None:
    scanned = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=1,
                text="",
                character_count=0,
            ),
        ),
        character_count=0,
        scanned_candidate=True,
    )

    assert _needs_recognition(scanned)

    readable = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=1,
                text="x" * 4_000,
                character_count=4_000,
            ),
        ),
        character_count=4_000,
        scanned_candidate=False,
    )

    assert not _needs_recognition(readable)


def test_a_markup_document_is_never_a_recognition_candidate() -> None:
    markup = ParsedDocument(
        document_format=DocumentFormat.HTML,
        parser_name="selectolax",
        pages=(
            ParsedPage(
                page_number=None,
                text="",
                character_count=0,
            ),
        ),
        character_count=0,
        scanned_candidate=False,
    )

    assert not _needs_recognition(markup)


def test_recognition_availability_is_reported_rather_than_assumed() -> None:
    """The caller learns up front whether Tesseract is installed."""

    assert isinstance(ocr_available(), bool)
