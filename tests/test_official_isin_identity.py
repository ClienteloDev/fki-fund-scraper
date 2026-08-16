"""The narrow official-ISIN identity rule.

A subfund KID prices one share class and prints its ISIN, but it rarely repeats the parent fund's
full legal name. Name-based identity therefore refuses it, which is what `ENTITY_NOT_MATCHED`
counted. An official ISIN settles ownership on its own - and nothing else.
"""

from __future__ import annotations

from fundscraper.field_extraction import (
    IsinIdentity,
    SourceScope,
    classify_source_scope,
    official_isin_scope,
)

FUND = "CCM SICAV II a.s."
OTHER_FUND = "CCM SICAV a.s."

INDEX = {
    "CZ1005202042": IsinIdentity(
        fund_name=FUND, scope=SourceScope.SHARE_CLASS, share_class_name="IAA"
    ),
    "CZ1005202000": IsinIdentity(fund_name=FUND, scope=SourceScope.EXACT_FUND),
    "CZ0008053303": IsinIdentity(
        fund_name=OTHER_FUND, scope=SourceScope.SHARE_CLASS, share_class_name="IAA"
    ),
}

# The real shape of the evidence: a KID that names the class ISIN and never the fund's legal name.
KID_TEXT = (
    "Sdělení klíčových informací. Investiční horizont investora: min. 7 let. "
    "ISIN CZ1005202042. Administrátorem fondu je DELTA Investiční společnost, a.s."
)


def test_official_isin_identifies_a_source_that_never_names_the_fund() -> None:
    assert (
        official_isin_scope(
            fund_name=FUND,
            source_url="https://deltais.cz/media/pages/20250902_kiid_iaa.pdf",
            source_title=None,
            document_text=KID_TEXT,
            isin_identity=INDEX,
        )
        is SourceScope.SHARE_CLASS
    )

    assert (
        classify_source_scope(
            fund_name=FUND,
            source_url="https://deltais.cz/media/pages/20250902_kiid_iaa.pdf",
            source_title=None,
            document_text=KID_TEXT,
            quote="Investiční horizont investora: min. 7 let",
            isin_identity=INDEX,
        )
        is SourceScope.SHARE_CLASS
    )


def test_an_isin_of_another_fund_never_asserts_this_fund() -> None:
    """The neighbouring case that must keep failing: a KID of a sibling fund."""

    assert (
        official_isin_scope(
            fund_name=FUND,
            source_url="https://deltais.cz/media/pages/2024_1007_kiid_iaa.pdf",
            source_title=None,
            document_text="Sdělení klíčových informací. ISIN CZ0008053303.",
            isin_identity=INDEX,
        )
        is None
    )


def test_without_an_index_or_an_isin_nothing_changes() -> None:
    for index, text in (
        (None, KID_TEXT),
        (INDEX, "Sdělení klíčových informací bez identifikátoru."),
        (INDEX, "ISIN CZ9999999999 patří jinému produktu."),
        ({}, KID_TEXT),
    ):
        assert (
            official_isin_scope(
                fund_name=FUND,
                source_url="https://example.test/kid.pdf",
                source_title=None,
                document_text=text,
                isin_identity=index,
            )
            is None
        )


def test_the_narrowest_scope_wins_when_a_document_prints_several_classes() -> None:
    """A statute lists every class, so it must stay the narrowest scope it proves."""

    assert (
        official_isin_scope(
            fund_name=FUND,
            source_url="https://deltais.cz/statut.pdf",
            source_title=None,
            document_text="Třídy: ISIN CZ1005202000, ISIN CZ1005202042.",
            isin_identity=INDEX,
        )
        is SourceScope.SHARE_CLASS
    )


def test_a_manager_statement_still_wins_over_the_isin() -> None:
    """Identity does not defeat semantics: a manager-level quote is still manager-level."""

    assert (
        classify_source_scope(
            fund_name=FUND,
            source_url="https://deltais.cz/vyrocni-zprava-spolecnosti.pdf",
            source_title="Výroční zpráva společnosti",
            document_text=KID_TEXT,
            quote="Celkový objem aktiv ve správě skupiny dosáhl 12 mld. Kč",
            isin_identity=INDEX,
        )
        is SourceScope.MANAGER
    )


def test_the_isin_index_is_off_by_default() -> None:
    """Every existing caller passes no index, so shipped behaviour is unchanged."""

    without = classify_source_scope(
        fund_name=FUND,
        source_url="https://deltais.cz/media/pages/20250902_kiid_iaa.pdf",
        source_title=None,
        document_text=KID_TEXT,
        quote="Investiční horizont investora: min. 7 let",
    )

    assert without is not SourceScope.SHARE_CLASS
