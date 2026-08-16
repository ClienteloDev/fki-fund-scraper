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


# The parameter block of a fund homepage, as a real site publishes it:
# a two-column table whose label and value sit in sibling cells.
FUND_PARAMETER_TABLE = """
<!doctype html>
<html>
  <body>
    <h2>Zakladni parametry</h2>
    <table>
      <tr>
        <td><span><b>Minimalni investice klienta</b></span></td>
        <td><span>1 mil. Kc</span></td>
      </tr>
      <tr>
        <td><b>Vstupni poplatek</b></td>
        <td>az 3 %</td>
      </tr>
      <tr>
        <td><b>Vystupni poplatek</b></td>
        <td>0 % po 3 letech, 5 % do 3 let</td>
      </tr>
    </table>
  </body>
</html>
"""


def test_html_table_rows_are_kept_as_a_grid() -> None:
    """
    A key/value block flattened to text loses which value belongs to which label.

    The fee extractor pairs a rate with its label by row, so an HTML page
    that publishes its parameters as a table produced no fee at all while
    the same table in a PDF produced one.
    """

    result = parse_document(
        body=FUND_PARAMETER_TABLE.encode("utf-8"),
        content_type="text/html",
        url="https://example.com/",
    )

    tables = [table for _, table in result.iter_tables()]

    assert len(tables) == 1

    assert tables[0].rows == (
        ("Minimalni investice klienta", "1 mil. Kc"),
        ("Vstupni poplatek", "az 3 %"),
        ("Vystupni poplatek", "0 % po 3 letech, 5 % do 3 let"),
    )


def test_html_layout_table_without_a_row_relationship_is_not_kept() -> None:
    """A single-column table carries no label-to-value relationship."""

    body = b"""
    <!doctype html>
    <html><body>
      <table><tr><td>Only one column</td></tr><tr><td>and another</td></tr></table>
    </body></html>
    """

    result = parse_document(
        body=body,
        content_type="text/html",
        url="https://example.com/",
    )

    assert result.table_count == 0


def test_html_tables_survive_the_parsed_cache() -> None:
    """The grid has to reach extraction, which reads the stored copy."""

    from fundscraper.document_parser import ParsedDocument

    result = parse_document(
        body=FUND_PARAMETER_TABLE.encode("utf-8"),
        content_type="text/html",
        url="https://example.com/",
    )

    reloaded = ParsedDocument.from_json_dict(result.to_json_dict())

    assert [table.rows for _, table in reloaded.iter_tables()] == [
        table.rows for _, table in result.iter_tables()
    ]
