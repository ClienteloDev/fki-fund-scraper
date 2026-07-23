from __future__ import annotations

from pathlib import Path
from typing import cast

import pymupdf
import pytest

from fundscraper.document_parser import (
    DocumentFormat,
    UnsupportedDocumentError,
    load_parsed_document,
    parse_document,
    write_parsed_document,
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


def create_empty_pdf() -> bytes:
    document = pymupdf.open()  # type: ignore[no-untyped-call]

    try:
        document.new_page()

        result = cast(
            bytes,
            document.tobytes(),  # type: ignore[no-untyped-call]
        )

        return result
    finally:
        document.close()  # type: ignore[no-untyped-call]


def test_parses_pdf_with_page_number() -> None:
    body = create_text_pdf("Recommended holding period 5 years")

    result = parse_document(
        body=body,
        content_type="application/pdf",
        url="https://example.com/kid.pdf",
    )

    assert result.document_format is DocumentFormat.PDF

    assert result.page_count == 1

    assert result.pages[0].page_number == 1

    assert "Recommended holding period" in result.full_text

    assert result.scanned_candidate is False


def test_empty_pdf_is_scanned_candidate() -> None:
    result = parse_document(
        body=create_empty_pdf(),
        content_type="application/pdf",
        url="https://example.com/scanned.pdf",
    )

    assert result.page_count == 1
    assert result.character_count == 0
    assert result.scanned_candidate is True


def test_parses_visible_html_text() -> None:
    body = b"""
    <!doctype html>
    <html>
      <head>
        <title>Example</title>
        <style>.hidden { display: none; }</style>
      </head>
      <body>
        <script>ignoredScript()</script>
        <h1>Example Fund</h1>
        <p>Minimum investment: 1 000 000 CZK</p>
      </body>
    </html>
    """

    result = parse_document(
        body=body,
        content_type="text/html",
        url="https://example.com/fund",
    )

    assert result.document_format is DocumentFormat.HTML

    assert "Example Fund" in result.full_text

    assert "Minimum investment" in result.full_text

    assert "ignoredScript" not in result.full_text


def test_rejects_unsupported_binary_document() -> None:
    with pytest.raises(
        UnsupportedDocumentError,
        match="could not be determined",
    ):
        parse_document(
            body=b"\x00\x01\x02\x03",
            content_type="application/octet-stream",
            url="https://example.com/file.bin",
        )


def test_writes_and_loads_parsed_document(
    tmp_path: Path,
) -> None:
    parsed = parse_document(
        body=b"<html><body>Fund text</body></html>",
        content_type="text/html",
        url="https://example.com",
    )

    output_path = write_parsed_document(
        directory=tmp_path,
        fund_id="fund_0123456789abcdef",
        source_id=10,
        document=parsed,
    )

    loaded = load_parsed_document(output_path)

    assert loaded == parsed
    assert output_path.exists()
