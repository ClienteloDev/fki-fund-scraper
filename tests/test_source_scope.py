"""
Regression tests for source-to-fund attribution.

Every case below reproduces a shared source that the output audit found
being used for several unrelated funds at once.
"""

from __future__ import annotations

from fundscraper.field_extraction import (
    SourceScope,
    classify_source_scope,
    extract_fund_fields,
)
from fundscraper.output_models import FieldStatus, ReasonCode, ScopeType
from tests.test_field_extraction import create_extraction_document

# The memorandum of ECFS Credit Fund, hosted on the shared Natland site.
ECFS_MEMORANDUM_URL = (
    "https://www.natland.cz/wp-content/uploads/2022/10/memorandum_ecfs_final_24.6.2021_na-web.pdf"
)

ECFS_MEMORANDUM_TEXT = """
ECFS Credit Fund SICAV, a.s.
Investicni memorandum

Cilovy vynos fondu je 8 % p.a.
Minimalni investice cini 1 000 000 Kc.
""".strip()


# One subfund page of 4 GIMEL Investments on the administrator website.
GIMEL_PAGE_URL = (
    "https://www.avantfunds.cz/fondy/4-gimel-investments-sicav-a-s/podfond-alfa-4-gimel/"
)

GIMEL_PAGE_TEXT = """
4 GIMEL Investments SICAV a.s.
Podfond ALFA

Doporuceny investicni horizont je 5 let.
Cilovy vynos je 10 % p.a.
""".strip()


# One PRIIPS document of a single CREDITAS fund, reused for its siblings.
CREDITAS_PRIIPS_URL = "https://www.creditas.cz/files/250501-priips-max-variant-final.pdf"

CREDITAS_PRIIPS_TEXT = """
Sdeleni klicovych informaci
CREDITAS ASSETS SICAV a.s.

Doporuceny investicni horizont je 5 let.
""".strip()


def scope_of(
    *,
    fund_name: str,
    url: str,
    title: str,
    text: str,
    quote: str = "",
) -> SourceScope:
    return classify_source_scope(
        fund_name=fund_name,
        source_url=url,
        source_title=title,
        document_text=text,
        quote=quote or text,
    )


def test_memorandum_of_another_fund_is_not_attributed_to_the_host_fund() -> None:
    """The ECFS memorandum must not supply values for Natland Real Estate."""

    assert (
        scope_of(
            fund_name="Natland Real Estate SICAV, a.s.",
            url=ECFS_MEMORANDUM_URL,
            title="Investicni memorandum",
            text=ECFS_MEMORANDUM_TEXT,
        )
        is SourceScope.OTHER_FUND
    )

    assert (
        scope_of(
            fund_name="ECFS Credit Fund SICAV, a.s.",
            url=ECFS_MEMORANDUM_URL,
            title="Investicni memorandum",
            text=ECFS_MEMORANDUM_TEXT,
        )
        is SourceScope.EXACT_FUND
    )


def test_subfund_page_of_another_fund_is_rejected() -> None:
    """The 4 GIMEL subfund page was used for ARBITAS, TOLAR and others."""

    for fund_name in (
        "ARBITAS SICAV, a.s.",
        "TOLAR SICAV a. s.",
        "EQUISOL SICAV, a.s.",
        "AVANT Finance SICAV a. s.",
    ):
        assert (
            scope_of(
                fund_name=fund_name,
                url=GIMEL_PAGE_URL,
                title="Podfond ALFA",
                text=GIMEL_PAGE_TEXT,
            )
            is SourceScope.OTHER_FUND
        )


def test_subfund_page_is_accepted_for_its_own_fund() -> None:
    scope = scope_of(
        fund_name="4 GIMEL Investments SICAV a.s.",
        url=GIMEL_PAGE_URL,
        title="Podfond ALFA",
        text=GIMEL_PAGE_TEXT,
    )

    assert scope is SourceScope.SUBFUND


def test_shared_priips_document_serves_only_the_fund_it_names() -> None:
    assert (
        scope_of(
            fund_name="CREDITAS ASSETS SICAV a.s.",
            url=CREDITAS_PRIIPS_URL,
            title="PRIIPS",
            text=CREDITAS_PRIIPS_TEXT,
        )
        is SourceScope.EXACT_FUND
    )

    for fund_name in (
        "GAMA SICAV, a.s.",
        "CREDITAS fond SICAV, a.s.",
    ):
        assert (
            scope_of(
                fund_name=fund_name,
                url=CREDITAS_PRIIPS_URL,
                title="PRIIPS",
                text=CREDITAS_PRIIPS_TEXT,
            )
            is not SourceScope.EXACT_FUND
        )


# A manager page that presents one fund but lists its siblings in the
# navigation, like https://www.bhs.cz/fond-ikonickych-automobilu.
BHS_PAGE_URL = "https://www.bhs.cz/fond-ikonickych-automobilu"

BHS_PAGE_TEXT = """
BHS Fondy

BHS DYNAMIC FUND SICAV, a.s.
BHS REAL ESTATE FUND SICAV, a.s.

BHS ICONIC CARS SICAV, a.s.
Doporuceny investicni horizont je 7 let.
Minimalni investice cini 2 000 000 Kc.
""".strip()


# A manager homepage presenting two funds one after another.
ONECAP_PAGE_URL = "https://www.onecap.cz/en/"

ONECAP_PAGE_TEXT = """
OneCap

OneCap Private Equity SICAV, a.s.
Cilovy vynos je 12 % p.a.

OneCap Real Estate & Infrastructure SICAV, a.s.
Cilovy vynos je 6 % p.a.
""".strip()


def test_multi_fund_manager_page_serves_only_the_named_section() -> None:
    """The BHS page names four funds; only its own section counts."""

    document = create_extraction_document(
        source_id=1,
        url=BHS_PAGE_URL,
        title="Fond ikonickych automobilu",
        text=BHS_PAGE_TEXT,
        document_type="marketing_page",
    )

    owner = extract_fund_fields(
        fund_name="BHS ICONIC CARS SICAV, a.s.",
        documents=[document],
    )

    assert owner.investment_horizon.status is FieldStatus.FOUND
    assert owner.investment_horizon.value is not None
    assert owner.investment_horizon.value.recommended_years == 7

    for fund_name in (
        "BHS DYNAMIC FUND SICAV, a.s.",
        "BHS REAL ESTATE FUND SICAV, a.s.",
    ):
        neighbour = extract_fund_fields(
            fund_name=fund_name,
            documents=[document],
        )

        assert neighbour.investment_horizon.status is FieldStatus.AMBIGUOUS
        assert neighbour.investment_horizon.value is None

        assert neighbour.minimum_investment.status is FieldStatus.AMBIGUOUS
        assert neighbour.minimum_investment.value is None


def test_each_fund_of_a_two_fund_page_keeps_its_own_value() -> None:
    """Both OneCap funds are named; each must keep only its own return."""

    document = create_extraction_document(
        source_id=1,
        url=ONECAP_PAGE_URL,
        title="OneCap",
        text=ONECAP_PAGE_TEXT,
        document_type="marketing_page",
    )

    private_equity = extract_fund_fields(
        fund_name="OneCap Private Equity SICAV, a.s.",
        documents=[document],
    )

    assert private_equity.target_return.status is FieldStatus.FOUND
    assert private_equity.target_return.value is not None
    assert private_equity.target_return.value.value_percent_pa == 12

    real_estate = extract_fund_fields(
        fund_name="OneCap Real Estate & Infrastructure SICAV, a.s.",
        documents=[document],
    )

    assert real_estate.target_return.status is FieldStatus.FOUND
    assert real_estate.target_return.value is not None
    assert real_estate.target_return.value.value_percent_pa == 6


def test_multi_fund_page_rejects_a_fund_it_does_not_list() -> None:
    document = create_extraction_document(
        source_id=1,
        url=BHS_PAGE_URL,
        title="Fond ikonickych automobilu",
        text=BHS_PAGE_TEXT,
        document_type="marketing_page",
    )

    result = extract_fund_fields(
        fund_name="Natland Real Estate SICAV, a.s.",
        documents=[document],
    )

    assert result.investment_horizon.status is FieldStatus.AMBIGUOUS
    assert result.investment_horizon.reason is not None
    assert result.investment_horizon.reason.code is ReasonCode.ENTITY_NOT_MATCHED


def test_single_fund_page_still_uses_whole_page_scope() -> None:
    """A page presenting one fund must keep working as before."""

    text = "\n".join(
        (
            "BHS ICONIC CARS SICAV, a.s.",
            "Investicni strategie fondu.",
            "Doporuceny investicni horizont je 7 let.",
        )
    )

    document = create_extraction_document(
        source_id=1,
        url=BHS_PAGE_URL,
        title="Fond ikonickych automobilu",
        text=text,
        document_type="marketing_page",
    )

    result = extract_fund_fields(
        fund_name="BHS ICONIC CARS SICAV, a.s.",
        documents=[document],
    )

    assert result.investment_horizon.status is FieldStatus.FOUND
    assert result.investment_horizon.value is not None
    assert result.investment_horizon.value.recommended_years == 7


def test_one_fund_named_several_ways_is_not_a_multi_fund_document() -> None:
    """
    A statute names its fund on almost every page, in varying wording.

    Treating those repetitions as different funds made the section rule
    reject values of a document that belongs to exactly one fund.
    """

    text = "\n".join(
        (
            "Statut KOOR ESG fond, podfond",
            "1.9 Investicni horizont: 5 let a vice.",
            "Minimalni investice cini 1 000 000 Kc.",
            "KOOR ESG SICAV a.s. je fond kvalifikovanych investoru.",
        )
    )

    document = create_extraction_document(
        source_id=1,
        url="https://www.koorfond.cz/statut-koor-esg.pdf",
        title="Statut",
        text=text,
        document_type="statute",
    )

    result = extract_fund_fields(
        fund_name="KOOR ESG SICAV a.s.",
        documents=[document],
    )

    assert result.investment_horizon.status is FieldStatus.FOUND
    assert result.investment_horizon.value is not None
    assert result.investment_horizon.value.recommended_years == 5

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 1_000_000


def test_repeated_own_name_does_not_close_the_fund_section() -> None:
    """A neighbouring fund closes a section, a repeated own name does not."""

    text = "\n".join(
        (
            "OneCap Private Equity SICAV, a.s.",
            "OneCap Private Equity SICAV, a.s. je fond kvalifikovanych investoru.",
            "Cilovy vynos je 12 % p.a.",
            "OneCap Real Estate & Infrastructure SICAV, a.s.",
            "Cilovy vynos je 6 % p.a.",
        )
    )

    document = create_extraction_document(
        source_id=1,
        url=ONECAP_PAGE_URL,
        title="OneCap",
        text=text,
        document_type="marketing_page",
    )

    private_equity = extract_fund_fields(
        fund_name="OneCap Private Equity SICAV, a.s.",
        documents=[document],
    )

    assert private_equity.target_return.status is FieldStatus.FOUND
    assert private_equity.target_return.value is not None
    assert private_equity.target_return.value.value_percent_pa == 12


def test_document_naming_no_fund_is_generic() -> None:
    assert (
        scope_of(
            fund_name="Natland Real Estate SICAV, a.s.",
            url="https://www.natland.cz/files/ceník-sluzeb.pdf",
            title="Cenik sluzeb",
            text="Cenik sluzeb\nVstupni poplatek cini 3 %.",
        )
        is SourceScope.GENERIC
    )


def test_manager_level_statement_is_manager_scope() -> None:
    assert (
        scope_of(
            fund_name="Example Fund SICAV a.s.",
            url="https://manager.example.com/o-nas",
            title="O nas",
            text="Skupina spravuje vsechny fondy.",
            quote="Skupina spravuje vsechny fondy. Majetek 10 mld. Kc.",
        )
        is SourceScope.MANAGER
    )


def test_values_from_another_funds_document_are_rejected_end_to_end() -> None:
    """The whole extraction must refuse a foreign document, not rank it."""

    document = create_extraction_document(
        source_id=1,
        url=ECFS_MEMORANDUM_URL,
        title="Investicni memorandum",
        text=ECFS_MEMORANDUM_TEXT,
    )

    result = extract_fund_fields(
        fund_name="Natland Real Estate SICAV, a.s.",
        documents=[document],
    )

    for field in (
        result.target_return,
        result.minimum_investment,
    ):
        assert field.status is FieldStatus.AMBIGUOUS
        assert field.value is None
        assert field.reason is not None
        assert field.reason.code is ReasonCode.ENTITY_NOT_MATCHED

    # The rejected source is still reported for review.
    assert result.target_return.attempted_sources


def test_values_from_the_matching_document_remain_accepted() -> None:
    document = create_extraction_document(
        source_id=1,
        url=ECFS_MEMORANDUM_URL,
        title="Investicni memorandum",
        text=ECFS_MEMORANDUM_TEXT,
    )

    result = extract_fund_fields(
        fund_name="ECFS Credit Fund SICAV, a.s.",
        documents=[document],
    )

    assert result.target_return.status is FieldStatus.FOUND
    assert result.target_return.value is not None
    assert result.target_return.value.value_percent_pa == 8

    assert result.target_return.scope is not None
    assert result.target_return.scope.type is ScopeType.FUND


def test_generic_document_no_longer_supplies_values() -> None:
    """A document proving no entity may not be attributed to a fund."""

    document = create_extraction_document(
        source_id=1,
        url="https://shared.example.com/files/prehled.pdf",
        title="Prehled",
        text="Prehled produktu\nMinimalni investice cini 1 000 000 Kc.",
    )

    result = extract_fund_fields(
        fund_name="Natland Real Estate SICAV, a.s.",
        documents=[document],
    )

    assert result.minimum_investment.status is FieldStatus.AMBIGUOUS
    assert result.minimum_investment.reason is not None
    assert result.minimum_investment.reason.code is ReasonCode.ENTITY_NOT_MATCHED


# A manager detail page: the fund is the subject, its siblings appear
# only in the navigation of the site.
CODYA_PAGE_URL = "https://www.codyainvest.cz/nase-fondy/care-sicav-a-s"

CODYA_PAGE_TEXT = "\n".join(
    (
        "KOOR ESG SICAV a.s.",
        "Adversum SICAV, a.s.",
        "CARE SICAV, a.s.",
        "Minimalni investice cini 1 000 000 Kc.",
    )
)


def test_fund_owns_the_page_filed_under_its_own_slug() -> None:
    """The navigation of a manager site must not disown its detail page."""

    document = create_extraction_document(
        source_id=1,
        url=CODYA_PAGE_URL,
        title="CARE SICAV, a.s.",
        text=CODYA_PAGE_TEXT,
        document_type="marketing_page",
    )

    result = extract_fund_fields(
        fund_name="CARE SICAV, a.s.",
        documents=[document],
    )

    assert result.minimum_investment.status is FieldStatus.FOUND
    assert result.minimum_investment.value is not None
    assert result.minimum_investment.value.amount == 1_000_000


def test_owning_the_page_still_rejects_a_neighbouring_fund_section() -> None:
    text = "\n".join(
        (
            "CARE SICAV, a.s.",
            "KOOR ESG SICAV a.s.",
            "Minimalni investice cini 5 000 000 Kc.",
        )
    )

    document = create_extraction_document(
        source_id=1,
        url=CODYA_PAGE_URL,
        title="CARE SICAV, a.s.",
        text=text,
        document_type="marketing_page",
    )

    result = extract_fund_fields(
        fund_name="CARE SICAV, a.s.",
        documents=[document],
    )

    assert result.minimum_investment.status is not FieldStatus.FOUND


def test_similar_subfund_slug_does_not_grant_ownership() -> None:
    """ "esg-seniorcare" and "alca-podfond-caresort" are not CARE SICAV."""

    from fundscraper.field_extraction import fund_identity_tokens, url_identifies_fund

    tokens = fund_identity_tokens("CARE SICAV, a.s.")

    assert url_identifies_fund(
        fund_tokens=tokens,
        source_url=CODYA_PAGE_URL,
    )

    for foreign_url in (
        "https://www.codyainvest.cz/nase-fondy/esg-seniorcare-trida-pia",
        "https://www.avantfunds.cz/wp-content/uploads/fondy/alca-podfond-caresort/x.pdf",
        "https://www.codyainvest.cz/nase-fondy/koor-esg-sicav-a-s",
    ):
        assert not url_identifies_fund(
            fund_tokens=tokens,
            source_url=foreign_url,
        )
