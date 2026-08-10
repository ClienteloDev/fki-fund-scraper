from __future__ import annotations

from datetime import date

from fundscraper.document_classification import (
    ClassificationSignals,
    classify_document,
    file_name_text,
)
from fundscraper.document_dates import (
    DateOrigin,
    extract_document_dates,
)
from fundscraper.document_identity import (
    DocumentScope,
    IdentityInput,
    resolve_document_identity,
)
from fundscraper.output_models import DocumentType

FUND_NAME = "Rezidento Alfa SICAV, a.s."


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classifies_a_key_information_document_from_its_body() -> None:
    result = classify_document(
        ClassificationSignals(
            url="https://www.rezidentoalfa.cz/file/sdff-get?id=7371",
            header_text=(
                "Sdělení klíčových informací\nÚčel\nTento dokument vám poskytuje "
                "klíčové informace o tomto investičním produktu."
            ),
        )
    )

    assert result.document_type is DocumentType.PRIIPS_KID

    assert "sdeleni klicovych informaci" in result.matched_keywords


def test_classifies_from_the_file_name_when_the_body_is_silent() -> None:
    result = classify_document(
        ClassificationSignals(
            url="https://www.rezidentoalfa.cz/dokumenty/vyrocni-zprava-2024.pdf",
        )
    )

    assert result.document_type is DocumentType.ANNUAL_REPORT


def test_separates_a_subfund_statute_from_a_fund_statute() -> None:
    subfund = classify_document(
        ClassificationSignals(
            url="https://www.rezidentoalfa.cz/dokumenty/statut.pdf",
            header_text="STATUT PODFONDU Rezidento Alfa",
        )
    )

    assert subfund.document_type is DocumentType.SUBFUND_STATUTE

    fund = classify_document(
        ClassificationSignals(
            url="https://www.rezidentoalfa.cz/dokumenty/statut.pdf",
            header_text="STATUT FONDU Rezidento Alfa SICAV, a.s.",
        )
    )

    assert fund.document_type is DocumentType.STATUTE


def test_the_word_subfund_alone_does_not_make_a_statute() -> None:
    """Nearly every document of a SICAV names its subfund somewhere."""

    result = classify_document(
        ClassificationSignals(
            url=("https://www.rezidentoalfa.cz/dokumenty/oznameni-o-ukonceni-upisu-podfond.pdf"),
            header_text="Oznámení investorům o ukončení úpisu podfondu",
        )
    )

    assert result.document_type is not DocumentType.SUBFUND_STATUTE

    assert result.document_type is DocumentType.INVESTOR_NOTICE


def test_a_passing_mention_of_the_register_does_not_make_a_register() -> None:
    result = classify_document(
        ClassificationSignals(
            url="https://www.rezidentoalfa.cz/dokumenty/stanovy.pdf",
            header_text=("Společnost je zapsána v obchodním rejstříku vedeném Městským soudem."),
        )
    )

    assert result.document_type is not DocumentType.REGISTER


def test_recognises_the_types_added_for_step_six() -> None:
    cases = {
        "Ceník poplatků fondu": DocumentType.PRICE_LIST,
        "Základní prospekt dluhopisového programu": DocumentType.PROSPECTUS,
        "Oznámení investorům o změně statutu": DocumentType.INVESTOR_NOTICE,
    }

    for header, expected in cases.items():
        result = classify_document(ClassificationSignals(header_text=header))

        assert result.document_type is expected, header


def test_reads_a_readable_file_name() -> None:
    assert file_name_text("https://x.cz/a/220701_statut_podfond-1.pdf") == "220701 statut podfond 1"


# ---------------------------------------------------------------------------
# Identity and scope
# ---------------------------------------------------------------------------


def test_attributes_a_document_naming_the_fund_on_its_title_page() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            url="https://www.spravce.cz/file/sdff-get?id=1",
            title_text="STATUT FONDU\nRezidento Alfa SICAV, a.s.",
        )
    )

    assert identity.scope is DocumentScope.EXACT_FUND

    assert identity.confidence == "high"

    assert identity.is_accepted


def test_refuses_a_document_of_another_fund_on_the_manager_domain() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            url="https://www.spravce.cz/dokumenty/statut.pdf",
            title_text="STATUT FONDU\nJiný fond SICAV, a.s.",
            body_text="Statut fondu Jiný fond SICAV, a.s.",
        )
    )

    assert identity.scope is DocumentScope.UNKNOWN

    assert not identity.is_accepted

    assert identity.rejection_reason is not None


def test_contradicting_evidence_is_reported_as_ambiguous() -> None:
    """Naming the fund and the manager at once resolves to neither."""

    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            url="https://www.spravce.cz/dokumenty/vyrocni-zprava.pdf",
            title_text="Výroční zpráva investiční společnosti",
            body_text=(
                "Výroční zpráva investiční společnosti obsahuje přehled fondů. "
                "Rezidento Alfa SICAV, a.s. je jedním z nich."
            ),
        )
    )

    assert identity.scope is DocumentScope.AMBIGUOUS

    assert not identity.is_accepted


def test_a_registration_number_confirms_the_fund() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name="Fond bez jmena",
            fund_ico="06876897",
            url="https://www.spravce.cz/dokumenty/statut.pdf",
            title_text="STATUT\nIČO: 068 76 897",
        )
    )

    assert identity.matched_ico == "06876897"

    assert identity.confidence == "high"


def test_a_different_registration_number_marks_another_fund() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name="Fond bez jmena",
            fund_ico="06876897",
            url="https://www.spravce.cz/dokumenty/statut.pdf",
            title_text="STATUT\nIČO: 27437558",
        )
    )

    assert identity.scope is DocumentScope.ANOTHER_FUND

    assert not identity.is_accepted


def test_a_share_class_document_is_scoped_to_its_class() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            title_text=("Rezidento Alfa SICAV, a.s.\nSdělení klíčových informací - třída PIA"),
        )
    )

    assert identity.scope is DocumentScope.SHARE_CLASS

    assert identity.share_class == "PIA"


def test_hosting_alone_never_attributes_a_document() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            url="https://www.spravce.cz/dokumenty/nejaky-soubor.pdf",
            title_text="Obecné informace",
        )
    )

    assert identity.scope is DocumentScope.UNKNOWN


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_reads_an_explicit_effective_date() -> None:
    dates = extract_document_dates(
        text="STATUT FONDU\nTento statut je účinný od 15. 12. 2025.",
    )

    assert dates.effective_at is not None

    assert dates.effective_at.value == date(2025, 12, 15)

    assert dates.effective_at.origin is DateOrigin.DOCUMENT_TEXT


def test_reads_an_issue_date_and_a_valuation_date() -> None:
    dates = extract_document_dates(
        text=("Měsíční zpráva\nÚdaje k 31. 8. 2024.\nVyhotoveno dne 10. 9. 2024."),
    )

    assert dates.as_of is not None

    assert dates.as_of.value == date(2024, 8, 31)

    assert dates.published_at is not None

    assert dates.published_at.value == date(2024, 9, 10)


def test_reads_a_reporting_period_written_as_a_span() -> None:
    dates = extract_document_dates(
        text="Výroční zpráva za období od 1.1.2024 do 31.12.2024",
    )

    assert dates.reporting_period_start is not None

    assert dates.reporting_period_start.value == date(2024, 1, 1)

    assert dates.reporting_period_end is not None

    assert dates.reporting_period_end.value == date(2024, 12, 31)


def test_derives_a_reporting_period_from_a_named_year() -> None:
    dates = extract_document_dates(text="Výroční zpráva za rok 2024")

    assert dates.reporting_period_end is not None

    assert dates.reporting_period_end.value == date(2024, 12, 31)

    assert dates.reporting_period_end.origin is DateOrigin.DERIVED


def test_prefers_a_date_inside_the_document_over_the_file_name() -> None:
    dates = extract_document_dates(
        text="Statut fondu\nVyhotoveno dne 3. 4. 2026.",
        url="https://x.cz/dokumenty/statut-2019.pdf",
    )

    assert dates.published_at is not None

    assert dates.published_at.value == date(2026, 4, 3)

    assert dates.published_at.origin is DateOrigin.DOCUMENT_TEXT


def test_marks_a_date_that_could_only_come_from_the_file_name() -> None:
    dates = extract_document_dates(
        text="Statut fondu bez data.",
        url="https://x.cz/dokumenty/statut-20240315.pdf",
    )

    assert dates.published_at is not None

    assert dates.published_at.value == date(2024, 3, 15)

    assert dates.published_at.origin is DateOrigin.FILE_NAME

    assert dates.published_at.confidence == "low"


def test_reads_an_english_year_end() -> None:
    dates = extract_document_dates(text="Annual report for the year ended 31.12.2024")

    assert dates.reporting_period_end is not None

    assert dates.reporting_period_end.value == date(2024, 12, 31)


def test_a_sentence_after_the_word_subfund_is_not_a_subfund_name() -> None:
    """A sentence continuing after "podfond" names no subfund."""

    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            title_text=("Rezidento Alfa SICAV, a.s.\nPodfond vydává investiční akcie třídy A."),
        )
    )

    assert identity.subfund_name is None


def test_a_real_subfund_name_is_kept() -> None:
    identity = resolve_document_identity(
        IdentityInput(
            fund_name=FUND_NAME,
            title_text="Rezidento Alfa SICAV, a.s.\nSTATUT PODFONDU ESG SeniorCARE",
        )
    )

    assert identity.subfund_name == "ESG SeniorCARE"

    assert identity.scope is DocumentScope.SUBFUND


def test_a_fragment_of_the_fund_name_is_not_a_subfund_name() -> None:
    """An all-capitals heading must not turn into a subfund."""

    from fundscraper.document_identity import _looks_like_a_name

    for fragment in (
        "Max Realitni Fond SICAV a",
        "Fund SICAV a",
        "Nazev:",
        "A",
    ):
        assert not _looks_like_a_name(fragment), fragment

    for name in (
        "ESG SeniorCARE",
        "EBM Residential podfond",
        "Defence",
    ):
        assert _looks_like_a_name(name), name
