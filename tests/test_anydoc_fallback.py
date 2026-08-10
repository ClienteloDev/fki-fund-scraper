from __future__ import annotations

from fundscraper.anydoc_fallback import (
    ANYDOC_FALLBACK_PARSER_NAME,
    FOREIGN_SECTION_TOLERANCE,
    FallbackOutcome,
    LayoutQuality,
    LayoutTrigger,
    apply_layout_fallback,
    attribute_pages,
    blocking_reason,
    broken_sentence_ratio,
    compare_parses,
    evidence_tokens,
    layout_triggers,
    measure_layout,
    retention,
)
from fundscraper.anydoc_parser import MarkdownChunk, is_anydoc_parser, markdown_chunks
from fundscraper.document_parser import (
    DocumentFormat,
    DocumentTable,
    ParsedDocument,
    ParsedPage,
    TextBlock,
)


def pdf_document(
    *,
    pages: list[ParsedPage],
    parser_name: str = "pymupdf",
) -> ParsedDocument:
    return ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name=parser_name,
        pages=tuple(pages),
        character_count=sum(page.character_count for page in pages),
        scanned_candidate=False,
    )


def page(
    number: int,
    text: str,
    *,
    tables: tuple[DocumentTable, ...] = (),
    blocks: tuple[TextBlock, ...] = (),
) -> ParsedPage:
    return ParsedPage(
        page_number=number,
        text=text,
        character_count=len(text),
        tables=tables,
        blocks=blocks,
    )


def column_blocks(count: int) -> tuple[TextBlock, ...]:
    """Return blocks laid out in two columns that do not overlap."""

    blocks: list[TextBlock] = []

    for index in range(count):
        left = index % 2 == 0

        blocks.append(
            TextBlock(
                order=index,
                x0=50.0 if left else 320.0,
                y0=float(index * 20),
                x1=280.0 if left else 550.0,
                y1=float(index * 20 + 15),
                text=f"blok cislo {index}",
            )
        )

    return tuple(blocks)


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------


def test_broken_sentence_ratio_sees_interleaved_columns() -> None:
    ordered = "Fond investuje. Investor drzi akcie. Spolecnost rozhoduje."

    interleaved = "Fond investuje. investor drzi akcie. spolecnost rozhoduje."

    assert broken_sentence_ratio(ordered) < broken_sentence_ratio(interleaved)


def test_broken_sentence_ratio_of_nothing_is_zero() -> None:
    assert broken_sentence_ratio("") == 0.0


def test_evidence_tokens_read_dates_years_and_rates() -> None:
    tokens = evidence_tokens("K 31. 12. 2024 byl poplatek 1,5 % a ISIN CZ0008474509.")

    assert "2024-12-31" in tokens.dates

    assert "2024" in tokens.years

    assert "1.5" in tokens.percentages

    assert "CZ0008474509" in tokens.isins


def test_retention_of_nothing_is_complete() -> None:
    assert retention(primary=frozenset(), candidate=frozenset()) == 1.0


def test_retention_counts_what_survived() -> None:
    assert (
        retention(
            primary=frozenset({"a", "b", "c", "d"}),
            candidate=frozenset({"a", "b"}),
        )
        == 0.5
    )


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_a_clean_parse_triggers_nothing() -> None:
    text = "Fond investuje do nemovitosti. " * 40

    document = pdf_document(
        pages=[
            page(
                1,
                text,
                tables=(DocumentTable(rows=(("Poplatek", "2 %"), ("Vstupni", "1 %"))),),
            )
        ]
    )

    assert layout_triggers(document=document) == ()


def test_a_rung_below_reading_order_triggers_the_fallback() -> None:
    document = pdf_document(
        pages=[page(1, "Fond investuje. " * 30)],
        parser_name="pymupdf+blocks",
    )

    assert LayoutTrigger.PARSER_LADDER_FALLBACK in layout_triggers(document=document)


def test_a_recognised_page_is_left_alone() -> None:
    # AnyDoc has no optical recognition, so a page that only gave its
    # text to the recogniser cannot be helped by it.
    document = pdf_document(
        pages=[page(1, "Fond investuje. " * 30)],
        parser_name="pymupdf+ocr",
    )

    assert LayoutTrigger.PARSER_LADDER_FALLBACK not in layout_triggers(document=document)


def test_text_without_a_usable_row_triggers_the_fallback() -> None:
    document = pdf_document(pages=[page(1, "Fond investuje do nemovitosti. " * 100)])

    assert LayoutTrigger.TEXT_WITHOUT_TABLES in layout_triggers(document=document)


def test_two_columns_trigger_the_fallback() -> None:
    document = pdf_document(
        pages=[
            page(
                1,
                "Fond investuje. " * 30,
                blocks=column_blocks(12),
            )
        ]
    )

    assert LayoutTrigger.MULTI_COLUMN_LAYOUT in layout_triggers(document=document)


def test_markup_is_never_triggered() -> None:
    document = ParsedDocument(
        document_format=DocumentFormat.HTML,
        parser_name="selectolax",
        pages=(ParsedPage(page_number=None, text="text " * 500, character_count=2500),),
        character_count=2500,
        scanned_candidate=False,
    )

    assert layout_triggers(document=document) == ()


# ---------------------------------------------------------------------------
# What is never handed over
# ---------------------------------------------------------------------------


def test_a_key_information_document_is_never_replaced() -> None:
    quality = measure_layout(pdf_document(pages=[page(1, "text")]))

    assert blocking_reason(document_type="priips_kid", quality=quality) is not None

    assert blocking_reason(document_type="financial_statements", quality=quality) is not None


def test_a_parse_holding_fee_rows_is_never_replaced() -> None:
    document = pdf_document(
        pages=[
            page(
                1,
                "Poplatky fondu",
                tables=(
                    DocumentTable(
                        rows=(
                            ("Vstupni poplatek", "2 %"),
                            ("Vystupni poplatek", "1 %"),
                        )
                    ),
                ),
            )
        ]
    )

    reason = blocking_reason(
        document_type="statute",
        quality=measure_layout(document),
    )

    assert reason is not None and "fee rows" in reason


def test_a_parse_holding_a_yearly_series_is_never_replaced() -> None:
    rows = tuple((f"Rok {year}", f"{year - 2000} %") for year in range(2016, 2024))

    document = pdf_document(
        pages=[page(1, "Vykonnost", tables=(DocumentTable(rows=rows),))],
    )

    reason = blocking_reason(
        document_type="annual_report",
        quality=measure_layout(document),
    )

    assert reason is not None and "per-year rows" in reason


def test_an_ordinary_statute_is_not_blocked() -> None:
    document = pdf_document(pages=[page(1, "Statut fondu. " * 50)])

    assert (
        blocking_reason(
            document_type="statute",
            quality=measure_layout(document),
        )
        is None
    )


# ---------------------------------------------------------------------------
# Comparing the two readings
# ---------------------------------------------------------------------------


def quality_of(
    text: str,
    *,
    tables: tuple[DocumentTable, ...] = (),
    fund: str | None = None,
) -> LayoutQuality:
    return measure_layout(
        pdf_document(pages=[page(1, text, tables=tables)]),
        fund_name=fund,
    )


def test_a_reading_that_loses_words_is_a_regression() -> None:
    primary = quality_of("slovo " * 200)

    candidate = quality_of("slovo " * 100)

    _, regressions = compare_parses(primary=primary, candidate=candidate)

    assert any("of the words" in reason for reason in regressions)


def test_a_reading_that_loses_dates_is_a_regression() -> None:
    primary = quality_of("K 31. 12. 2024 a k 30. 6. 2023 a k 31. 3. 2022 byl kapital.")

    candidate = quality_of("K 31. 12. 2024 byl kapital.")

    _, regressions = compare_parses(primary=primary, candidate=candidate)

    assert any("dates" in reason for reason in regressions)


def test_losing_a_single_identifier_is_a_regression() -> None:
    primary = quality_of("Trida A CZ0008474509 a trida B CZ0008474517 fondu.")

    candidate = quality_of("Trida A CZ0008474509 fondu.")

    _, regressions = compare_parses(primary=primary, candidate=candidate)

    assert any("identifiers" in reason for reason in regressions)


def test_more_rows_are_an_improvement() -> None:
    primary = quality_of("text " * 100)

    candidate = quality_of(
        "text " * 100,
        tables=(DocumentTable(rows=(("Nazev", "1"), ("Jiny", "2"))),),
    )

    improvements, regressions = compare_parses(primary=primary, candidate=candidate)

    assert improvements

    assert not regressions


def test_losing_the_running_header_costs_attribution() -> None:
    # A statute repeats its own name in the page header. A reading that
    # drops it leaves the mention of another fund running to the end of
    # the document, which is what refuses every value inside it.
    fund = "3M FUND MSI SICAV a.s."

    with_header = " ".join(
        f"3M FUND MSI SICAV a.s. Odstavec {index} o fondu. Jiny fond ABC SICAV investicni fond."
        for index in range(12)
    )

    without_header = " ".join(
        f"Odstavec {index} o fondu. Jiny fond ABC SICAV investicni fond." for index in range(12)
    )

    primary = quality_of(with_header, fund=fund)

    candidate = quality_of(without_header, fund=fund)

    assert (
        candidate.foreign_section_ratio > primary.foreign_section_ratio + FOREIGN_SECTION_TOLERANCE
    )

    _, regressions = compare_parses(primary=primary, candidate=candidate)

    assert any("another fund" in reason for reason in regressions)


# ---------------------------------------------------------------------------
# Page numbers
# ---------------------------------------------------------------------------


# Passages have to carry enough long words to be recognised; three is
# the floor the attribution uses, and a real paragraph carries far more.
PAGE_ONE = "Zakladni udaje o fondu a jeho obhospodarovateli, administratorovi a depozitari."

PAGE_TWO = "Poplatky uctovane investorovi pri vstupu, vystupu a prubezne behem drzeni."

PAGE_THREE = "Historicka vykonnost fondu za uplynula ucetni obdobi vcetne srovnavacich hodnot."


def test_a_conversion_is_numbered_from_the_primary_parse() -> None:
    primary = pdf_document(
        pages=[
            page(1, PAGE_ONE),
            page(2, PAGE_TWO),
            page(3, PAGE_THREE),
        ]
    )

    chunks = [
        MarkdownChunk(text=PAGE_ONE),
        MarkdownChunk(text=PAGE_TWO),
        MarkdownChunk(text=PAGE_THREE),
    ]

    rebuilt = attribute_pages(
        primary=primary,
        chunks=chunks,
        document_format=DocumentFormat.PDF,
    )

    assert [item.page_number for item in rebuilt.pages] == [1, 2, 3]

    assert rebuilt.parser_name == ANYDOC_FALLBACK_PARSER_NAME

    assert is_anydoc_parser(rebuilt.parser_name)


def test_a_passage_that_cannot_be_placed_keeps_no_page_number() -> None:
    primary = pdf_document(pages=[page(1, PAGE_ONE)])

    chunks = [
        MarkdownChunk(text=PAGE_ONE),
        MarkdownChunk(
            text="Naprosto nesouvisejici pasaz pojednavajici o zcela jinych zalezitostech."
        ),
    ]

    rebuilt = attribute_pages(
        primary=primary,
        chunks=chunks,
        document_format=DocumentFormat.PDF,
    )

    assert [item.page_number for item in rebuilt.pages] == [1, None]


def test_the_search_reaches_past_a_passage_it_could_not_place() -> None:
    # A cover page that matches nothing must not stop the pages after it
    # from being found, which is what a window that only moves on success
    # would do.
    topics = (
        "obhospodarovateli",
        "depozitari",
        "investicnich",
        "nemovitostech",
        "pohledavkach",
        "dividendach",
        "likviditou",
        "auditorovi",
        "danovymi",
        "vykonnosti",
    )

    primary = pdf_document(
        pages=[
            page(number, f"Kapitola pojednavajici o {topic} a souvisejicich pravidlech.")
            for number, topic in enumerate(topics, start=1)
        ]
    )

    chunks = [
        MarkdownChunk(text="Uvodni strana bez jakekoli shody s pozdejsim obsahem dokumentu."),
        MarkdownChunk(text=f"Kapitola pojednavajici o {topics[-1]} a souvisejicich pravidlech."),
    ]

    rebuilt = attribute_pages(
        primary=primary,
        chunks=chunks,
        document_format=DocumentFormat.PDF,
    )

    assert [item.page_number for item in rebuilt.pages] == [None, len(topics)]


def test_the_rebuilt_document_round_trips_its_counts() -> None:
    primary = pdf_document(pages=[page(1, "Poplatky fondu a jejich vyse pro investory.")])

    rebuilt = attribute_pages(
        primary=primary,
        chunks=markdown_chunks(
            "Poplatky fondu a jejich vyse pro investory.\n\n"
            "| Vstupni | 2 % |\n|---|---|\n| Vystupni | 1 % |"
        ),
        document_format=DocumentFormat.PDF,
    )

    assert rebuilt.character_count == sum(page.character_count for page in rebuilt.pages)

    assert rebuilt.table_count == 1


# ---------------------------------------------------------------------------
# The whole decision
# ---------------------------------------------------------------------------


def test_a_document_that_triggers_nothing_keeps_its_primary_parse() -> None:
    document = pdf_document(
        pages=[
            page(
                1,
                "Fond investuje do nemovitosti. " * 40,
                tables=(DocumentTable(rows=(("Nazev", "1"), ("Jiny", "2"))),),
            )
        ]
    )

    decision = apply_layout_fallback(
        body=b"%PDF-1.4",
        primary=document,
        document_type="statute",
        fund_name="Testovaci fond SICAV a.s.",
    )

    assert decision.outcome is FallbackOutcome.NOT_TRIGGERED

    assert decision.document is document

    assert not decision.accepted


def test_a_blocked_document_is_never_converted() -> None:
    document = pdf_document(
        pages=[page(1, "Naklady na vstup. " * 200)],
        parser_name="pymupdf+blocks",
    )

    decision = apply_layout_fallback(
        body=b"%PDF-1.4 not a real pdf",
        primary=document,
        document_type="priips_kid",
        fund_name="Testovaci fond SICAV a.s.",
    )

    assert decision.outcome is FallbackOutcome.BLOCKED

    assert decision.document is document

    # Nothing was converted, so nothing was spent.
    assert decision.conversion_seconds == 0.0


def test_a_replacement_is_refused_when_the_fund_is_unknown() -> None:
    document = pdf_document(
        pages=[page(1, "Fond investuje do nemovitosti. " * 100)],
    )

    decision = apply_layout_fallback(
        body=b"not a pdf at all",
        primary=document,
        document_type="statute",
    )

    # Either the conversion failed outright or it was refused for want of
    # the attribution check; neither may replace the primary parse.
    assert decision.outcome in {FallbackOutcome.FAILED, FallbackOutcome.REJECTED}

    assert decision.document is document
