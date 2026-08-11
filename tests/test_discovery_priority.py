from __future__ import annotations

from fundscraper.discovery_priority import (
    DOCUMENT_THRESHOLD,
    NAVIGATION_THRESHOLD,
    PrioritySignals,
    distinctive_tokens,
    document_type_rank,
    file_name_of,
    is_unrelated,
    score_link,
)
from fundscraper.output_models import DocumentType

FUND_NAME = "Rezidento Alfa SICAV, a.s."


def _score(
    url: str,
    *,
    anchor_text: str = "",
    page_title: str = "",
    fund_name: str = FUND_NAME,
    document_type: DocumentType | None = None,
) -> int:
    return score_link(
        PrioritySignals(
            url=url,
            anchor_text=anchor_text,
            page_title=page_title,
            fund_name=fund_name,
            document_type=document_type,
        )
    ).value


def test_ranks_the_wanted_document_types_in_order() -> None:
    ordered = (
        DocumentType.PRIIPS_KID,
        DocumentType.SUBFUND_STATUTE,
        DocumentType.STATUTE,
        DocumentType.FACTSHEET,
        DocumentType.ANNUAL_REPORT,
        DocumentType.FINANCIAL_STATEMENTS,
        DocumentType.MEMORANDUM,
    )

    ranks = [document_type_rank(item) for item in ordered]

    assert ranks == sorted(
        ranks,
        reverse=True,
    )

    assert document_type_rank(None) == 0


def test_reads_the_keywords_out_of_a_url_slug() -> None:
    assert (
        _score("https://www.rezidentoalfa.cz/dokumenty/sdeleni-klicovych-informaci-2024.pdf")
        >= DOCUMENT_THRESHOLD
    )

    assert _score("https://www.rezidentoalfa.cz/ke-stazeni/statut-fondu.pdf") >= DOCUMENT_THRESHOLD

    assert (
        _score("https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2024.pdf")
        >= DOCUMENT_THRESHOLD
    )


def test_recognises_the_investor_and_document_sections() -> None:
    for url in (
        "https://www.rezidentoalfa.cz/pro-investory/",
        "https://www.rezidentoalfa.cz/dokumenty/",
        "https://www.rezidentoalfa.cz/ke-stazeni/",
        "https://www.rezidentoalfa.cz/povinne-informace/",
    ):
        assert _score(url) >= NAVIGATION_THRESHOLD, url


def test_refuses_ordinary_corporate_pages() -> None:
    for url in (
        "https://www.rezidentoalfa.cz/kariera/",
        "https://www.rezidentoalfa.cz/ochrana-osobnich-udaju/",
        "https://www.rezidentoalfa.cz/cookies/",
    ):
        assert _score(url) < NAVIGATION_THRESHOLD, url

        assert is_unrelated(PrioritySignals(url=url))


def test_a_page_naming_the_fund_outranks_a_generic_one() -> None:
    named = _score(
        "https://www.spravce.cz/fondy/rezidento-alfa-sicav/dokumenty",
    )

    generic = _score(
        "https://www.spravce.cz/fondy/jiny-fond-sicav/dokumenty",
    )

    assert named > generic


def test_prefers_the_more_recent_version_of_one_document() -> None:
    recent = _score("https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2024.pdf")

    old = _score("https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2016.pdf")

    assert recent > old

    # The older version is still worth keeping: it carries the history
    # the annual returns and the value series are built from.
    assert old >= DOCUMENT_THRESHOLD


def test_a_detected_document_type_raises_the_score() -> None:
    without_type = _score("https://www.rezidentoalfa.cz/files/dokument.pdf")

    with_type = _score(
        "https://www.rezidentoalfa.cz/files/dokument.pdf",
        document_type=DocumentType.PRIIPS_KID,
    )

    assert with_type > without_type


def test_anchor_text_alone_can_identify_a_document() -> None:
    assert (
        _score(
            "https://www.rezidentoalfa.cz/files/download.php?id=42",
            anchor_text="Sdělení klíčových informací",
        )
        >= DOCUMENT_THRESHOLD
    )


def test_reads_a_readable_file_name() -> None:
    assert file_name_of("https://example.com/a/vyrocni-zprava-2024.pdf") == "vyrocni zprava 2024"


def test_generic_fund_words_are_not_distinctive() -> None:
    assert distinctive_tokens("Rezidento Alfa SICAV, a.s.") == (
        "rezidento",
        "alfa",
    )

    assert distinctive_tokens("investiční fond SICAV, a.s.") == ()


def test_a_wanted_document_type_outranks_an_unwanted_one_of_the_same_kind() -> None:
    """
    Field awareness is what makes the deep pass different from the fast one.

    A fund whose assets are missing is sent out for annual reports, and
    the report has to be fetched before the key information document the
    fast pass already read.
    """

    annual = PrioritySignals(
        url="https://www.examplefund.cz/dokumenty/vyrocni-zprava-2024.pdf",
        anchor_text="Výroční zpráva 2024",
        fund_name="Example Fund SICAV a.s.",
        document_type=DocumentType.ANNUAL_REPORT,
    )

    plain = score_link(annual)

    wanted = score_link(
        annual,
        wanted_document_types=frozenset({DocumentType.ANNUAL_REPORT}),
    )

    assert wanted.value > plain.value

    assert any(reason.startswith("wanted_document_type:") for reason in wanted.reasons)

    key_information = PrioritySignals(
        url="https://www.examplefund.cz/dokumenty/sdeleni-klicovych-informaci.pdf",
        anchor_text="Sdělení klíčových informací",
        fund_name="Example Fund SICAV a.s.",
        document_type=DocumentType.PRIIPS_KID,
    )

    # Without the bias the key information document ranks higher; with it
    # the annual report does.
    assert score_link(key_information).value > plain.value

    assert (
        wanted.value
        > score_link(
            key_information,
            wanted_document_types=frozenset({DocumentType.ANNUAL_REPORT}),
        ).value
    )
