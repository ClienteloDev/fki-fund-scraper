from __future__ import annotations

from pathlib import Path

from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentFormat,
    ParsedDocument,
    ParsedPage,
)
from fundscraper.extended_extraction import (
    extract_extended_fields,
    fold_aligned,
)
from fundscraper.field_extraction import ExtractionDocument
from fundscraper.output_models import (
    AumMetricType,
    FieldStatus,
    HistoricalValueType,
    NewsSourceType,
    ReturnSeriesType,
    SeriesFrequency,
)

FUND_NAME = "Rezidento Alfa SICAV, a.s."

FUND_WEB = "https://www.rezidentoalfa.cz"


def _document(
    *,
    text: str,
    url: str = "https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava.pdf",
    document_type: str = "annual_report",
    title: str | None = "Rezidento Alfa SICAV, a.s. - výroční zpráva",
    source_id: int = 1,
    local_path: str | None = None,
) -> ExtractionDocument:
    parsed = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=2,
                text=text,
                character_count=len(text),
            ),
        ),
        character_count=len(text),
        scanned_candidate=False,
    )

    record = ParsedDocumentRecord(
        source_id=source_id,
        fund_id="fund_0123456789abcdef",
        url=url,
        title=title,
        document_type=document_type,
        content_type="application/pdf",
        retrieved_at="2026-07-23T12:00:00+00:00",
        document_format="pdf",
        parser_name="pymupdf",
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path="cache/parsed/example.json",
        parsed_at="2026-07-23T12:01:00+00:00",
        local_path=local_path,
    )

    return ExtractionDocument(
        record=record,
        document=parsed,
    )


def test_folding_keeps_every_character_in_place() -> None:
    value = "Obhospodařovatelem je"

    folded = fold_aligned(value)

    assert len(folded) == len(value)

    assert folded.startswith("obhospodarovatelem")


def test_extracts_the_manager_and_the_administrator_of_one_sentence() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    1.5 Údaje o obhospodařovateli a administrátorovi
    Obhospodařovatelem a administrátorem Fondu je CODYA investiční společnost, a.s.,
    IČ: 068 76 897, se sídlem Lidická 1879/48, Brno.
    """.strip()

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[_document(text=text)],
    )

    assert result.manager.status is FieldStatus.FOUND

    assert result.manager.value is not None

    assert result.manager.value.name == "CODYA investiční společnost, a.s."

    assert result.manager.value.ico == "06876897"

    assert result.administrator.status is FieldStatus.FOUND

    assert result.administrator.value is not None

    assert result.administrator.value.name == "CODYA investiční společnost, a.s."


def test_keeps_the_registered_capital_out_of_the_assets_history() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Zapisovaný základní kapitál k 31. 12. 2024: 100 000 Kč
    """.strip()

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[_document(text=text)],
    )

    assert result.aum_history.status is FieldStatus.NOT_FOUND

    assert result.aum_history.reason is not None


def test_reads_a_decimal_amount_written_in_front_of_its_unit() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Majetek fondu k 31. 8. 2024: 1,888 mld. Kč
    """.strip()

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[_document(text=text)],
    )

    assert result.aum_history.status is FieldStatus.FOUND

    assert result.aum_history.value is not None

    observation = result.aum_history.value.observations[0]

    assert observation.amount == 1_888_000_000

    assert observation.metric_type is AumMetricType.ASSETS_TOTAL


def test_extracts_calendar_year_returns_and_refuses_the_other_periods() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Zhodnocení za rok 2023 +7,73 %
    Zhodnocení za rok 2024 +6,51 %
    Zhodnocení od založení +26,62 %
    """.strip()

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[_document(text=text)],
    )

    assert result.annual_returns.status is FieldStatus.FOUND

    assert result.annual_returns.value is not None

    observations = result.annual_returns.value.observations

    assert [item.year for item in observations] == [
        2023,
        2024,
    ]

    assert [item.return_percent for item in observations] == [
        7.73,
        6.51,
    ]

    assert all(item.series_type is ReturnSeriesType.CALENDAR_YEAR for item in observations)


def test_ignores_a_year_and_percentage_of_a_narrative_paragraph() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Výkonnost portfolia byla ovlivněna tím, že Česká národní banka pokračovala
    ve snižování sazeb, nicméně každé zasedání jen o 0,25 %, a to až do prosince,
    kdy inflace za rok 2024 dosáhla 2 % a proces měnové normalizace skončil.
    """.strip()

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[_document(text=text)],
    )

    assert result.annual_returns.status is FieldStatus.NOT_FOUND


def test_builds_one_value_series_per_measured_quantity() -> None:
    text = """
    Rezidento Alfa SICAV, a.s.

    Hodnota investiční akcie je vyhlašována měsíčně.
    Hodnota investiční akcie k 31. 5. 2024: 1,1949 Kč
    Hodnota investiční akcie k 30. 6. 2024: 1,2141 Kč
    """.strip()

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[_document(text=text)],
    )

    assert result.historical_values.status is FieldStatus.FOUND

    assert result.historical_values.value is not None

    series = result.historical_values.value.series

    assert len(series) == 1

    assert series[0].value_type is HistoricalValueType.INVESTMENT_SHARE_VALUE

    assert series[0].frequency is SeriesFrequency.MONTHLY

    assert [item.value for item in series[0].observations] == [
        1.1949,
        1.2141,
    ]


def test_reads_news_from_the_downloaded_page_of_the_fund(
    tmp_path: Path,
) -> None:
    body = """
    <html><body>
      <a href="/aktuality/">Aktuality</a>
      <a href="/aktuality/vyrocni-zprava-2024-rezidento-alfa">
        Výroční zpráva 2024 fondu Rezidento Alfa
      </a>
      <a href="/aktuality/2025/03/novy-projekt-v-brne">Nový projekt v&nbsp;Brně</a>
      <a href="/aktuality/">Zpět na přehled</a>
    </body></html>
    """.strip()

    local_path = tmp_path / "aktuality.body"

    local_path.write_text(
        body,
        encoding="utf-8",
    )

    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[
            _document(
                text="Rezidento Alfa SICAV, a.s.\nAktuality",
                url="https://www.rezidentoalfa.cz/aktuality/",
                document_type="marketing_page",
                title="Aktuality - Rezidento Alfa SICAV, a.s.",
                local_path=str(local_path),
            )
        ],
    )

    assert result.news.status is FieldStatus.FOUND

    assert result.news.value is not None

    titles = [item.title for item in result.news.value.items]

    assert "Výroční zpráva 2024 fondu Rezidento Alfa" in titles

    assert "Nový projekt v Brně" in titles

    assert "Zpět na přehled" not in titles

    assert all(item.source_type is NewsSourceType.OFFICIAL_FUND for item in result.news.value.items)

    dated = next(item for item in result.news.value.items if item.published_at is not None)

    assert dated.published_at is not None

    assert dated.published_at.isoformat() == "2025-03-01"


def test_reports_pending_shaped_results_without_documents() -> None:
    result = extract_extended_fields(
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        documents=[],
    )

    for field in (
        result.manager,
        result.administrator,
        result.aum_history,
        result.annual_returns,
        result.historical_values,
        result.news,
    ):
        assert field.status is FieldStatus.NOT_FOUND

        assert field.reason is not None

        assert field.value is None
