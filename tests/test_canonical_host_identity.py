"""
Regression tests for the canonical-host attribution defect found by online
recovery batch 01.

`classify_source_scope` ended with a fallback that counted how many of the
fund's distinctive words appeared anywhere in the title, the URL *including
its host*, and the first 5 000 characters of the page. A quorum of half of
them was enough to return `exact_fund`.

That made the crawl context itself the identity evidence. Both OneCap funds
share `onecap.cz`; the host supplied "onecap" and the English marketing copy
supplied "private", "equity", "real", "estate" and "infrastructure", so one
sentence about a strategy — on a page that states no legal name, no ISIN and
no IČO — was attributed to both of them as `target_return = 17 %`.

The rule these tests pin down: a shared host, the crawl assignment and a
scattering of the fund's words are inventory, never identity. `exact_fund`
needs the legal name, an ISIN, or an address that is the fund's own.
"""

from __future__ import annotations

from fundscraper.field_extraction import (
    IsinIdentity,
    IsinIdentityIndex,
    SourceScope,
    classify_source_scope,
    extract_fund_fields,
)
from fundscraper.output_models import FieldStatus
from tests.test_field_extraction import create_extraction_document

# Verbatim from the cached body of https://www.onecap.cz/en/ acquired for
# queue rank 2 (cache/recovery-online/002-onecap-private-equity/http). The
# string "SICAV" occurs nowhere on the page; the footer belongs to OneCap
# Partners s.r.o., IČ 239 95 165, which is neither fund.
ONECAP_CANONICAL_URL = "https://www.onecap.cz/en/"

ONECAP_CANONICAL_TEXT = " ".join(
    (
        "We deliver more than capital. At OneCap, we invest in special",
        "situations, build thematic platforms, and provide access to the most",
        "compelling real estate and infrastructure opportunities across Europe.",
        "We are a Czech private equity firm with a hands-on approach to",
        "investing. Strategic Investments Our flagship strategy focuses on",
        "building robust platforms in carefully selected industries.",
        "From opportunity to platform. From platform to market leader.",
        "STRATEGIC INVESTMENTS We are not aiming for quick exits - we build",
        "for long-term sustainability. We acquire and integrate companies into",
        "platforms operating in sectors shaped by strong, long-term",
        "macroeconomic trends. OneCap acquires and develops established",
        "businesses within a specific sector, often through follow-on",
        "horizontal or vertical acquisitions, with the goal of building a",
        "market leader. We target deal sizes between CZK 200-1,000 million,",
        "with an expected return of approximately 17%+ IRR. At the same time,",
        "we offer investors a semi-liquid fund structure that enables partial",
        "exits during the investment horizon.",
    )
)

ONECAP_QUOTE = (
    "We target deal sizes between CZK 200-1,000 million, with an expected "
    "return of approximately 17%+ IRR."
)

ONECAP_PRIVATE_EQUITY = "OneCap Private Equity SICAV, a.s."

ONECAP_REAL_ESTATE = "OneCap Real Estate & Infrastructure SICAV, a.s."


def scope_of(
    *,
    fund_name: str,
    url: str,
    text: str,
    title: str | None = None,
    quote: str = "",
    value_offset: int | None = None,
    isin_identity: IsinIdentityIndex | None = None,
) -> SourceScope:
    return classify_source_scope(
        fund_name=fund_name,
        source_url=url,
        source_title=title,
        document_text=text,
        quote=quote or text,
        value_offset=value_offset,
        isin_identity=isin_identity,
    )


def test_canonical_page_naming_no_fund_is_not_exact_fund() -> None:
    """
    The defect itself: `onecap.cz/en/` names neither fund, yet answered for both.

    The page is the canonical `web` of both funds, so the crawl reached it
    for each of them in turn. Nothing else connected the 17 % sentence to
    either one.
    """

    for fund_name in (
        ONECAP_PRIVATE_EQUITY,
        ONECAP_REAL_ESTATE,
    ):
        assert (
            scope_of(
                fund_name=fund_name,
                url=ONECAP_CANONICAL_URL,
                text=ONECAP_CANONICAL_TEXT,
                quote=ONECAP_QUOTE,
                value_offset=ONECAP_CANONICAL_TEXT.index("17%"),
            )
            is SourceScope.GENERIC
        )


def test_canonical_page_naming_no_fund_yields_no_value() -> None:
    """The whole extraction must refuse it, not only the scope call."""

    document = create_extraction_document(
        source_id=1,
        url=ONECAP_CANONICAL_URL,
        title="OneCap",
        text=ONECAP_CANONICAL_TEXT,
        document_type="marketing_page",
    )

    for fund_name in (
        ONECAP_PRIVATE_EQUITY,
        ONECAP_REAL_ESTATE,
    ):
        result = extract_fund_fields(
            fund_name=fund_name,
            documents=[document],
            fund_web="https://www.onecap.cz",
        )

        assert result.target_return.status is not FieldStatus.FOUND
        assert result.target_return.value is None


# The canonical `web` of MAVERICK Fund SICAV, a.s. is versuteis.cz, the site
# of Versute investiční společnost, a.s., IČO 08787131. The fund is named on
# none of its pages. This is the same shape as the OneCap defect with a
# single-token fund name, where the old quorum needed exactly one word.
VERSUTE_PAGE_URL = "https://www.versuteis.cz/o-nas/"

VERSUTE_PAGE_TEXT = "\n".join(
    (
        "Versute investicni spolecnost, a.s.",
        "IC 08787131",
        "Jsme nezavisla investicni spolecnost. Zajistujeme obhospodarovani",
        "a administraci fondu kvalifikovanych investoru.",
        "Minimalni investice cini 1 000 000 Kc.",
    )
)


def test_unrelated_entity_on_the_canonical_host_is_not_the_fund() -> None:
    """The canonical `web` presents the investment company, not the fund."""

    assert (
        scope_of(
            fund_name="MAVERICK Fund SICAV, a.s.",
            url=VERSUTE_PAGE_URL,
            title="O nas",
            text=VERSUTE_PAGE_TEXT,
            quote="Minimalni investice cini 1 000 000 Kc.",
            value_offset=VERSUTE_PAGE_TEXT.index("Minimalni investice"),
        )
        is not SourceScope.EXACT_FUND
    )


# A hub root: the fund's canonical `web` is the bare front page of its
# administrator, which serves every fund the house runs. Being crawled from
# there says nothing about which fund a figure belongs to.
AVANT_HUB_ROOT_URL = "https://www.avantfunds.cz/"

AVANT_HUB_ROOT_TEXT = "\n".join(
    (
        "AVANT investicni spolecnost, a.s.",
        "Nejvetsi sprava fondu kvalifikovanych investoru v Ceske republice.",
        "Informace o fondech",
        "Minimalni investice do fondu kvalifikovanych investoru cini",
        "1 000 000 Kc.",
    )
)


def test_shared_hub_root_alone_is_not_identity() -> None:
    """A hub front page is inventory; it attributes nothing to one fund."""

    assert (
        scope_of(
            fund_name="Numero Fund SICAV, a.s.",
            url=AVANT_HUB_ROOT_URL,
            title="AVANT investicni spolecnost",
            text=AVANT_HUB_ROOT_TEXT,
            quote="Minimalni investice do fondu kvalifikovanych investoru cini 1 000 000 Kc.",
            value_offset=AVANT_HUB_ROOT_TEXT.index("Minimalni investice"),
        )
        is not SourceScope.EXACT_FUND
    )


def test_host_carrying_the_fund_name_is_not_identity_on_its_own() -> None:
    """
    A host is not evidence even when it spells the fund out.

    `numerofund.cz` is registered to the fund, but a page on it that states
    no legal name is still only "somewhere on the fund's host". Accepting on
    the host alone is the rule that produced the OneCap misattribution, and
    it must not survive in the narrower case either.
    """

    text = "\n".join(
        (
            "Fondy kvalifikovanych investoru",
            "Minimalni investice do techto fondu obvykle cini 1 000 000 Kc.",
        )
    )

    assert (
        scope_of(
            fund_name="Numero Fund SICAV, a.s.",
            url="https://www.numerofund.cz/faq/",
            title="Casté dotazy",
            text=text,
            quote="Minimalni investice do techto fondu obvykle cini 1 000 000 Kc.",
            value_offset=text.index("Minimalni investice"),
        )
        is not SourceScope.EXACT_FUND
    )


# The other side of every rule above: what must keep passing.


def test_exact_legal_name_in_the_document_is_accepted() -> None:
    """The same page, with the fund's legal name printed on it."""

    text = "\n".join(
        (
            "OneCap Private Equity SICAV, a.s.",
            "Cilovy vynos fondu je 17 % p.a.",
        )
    )

    assert (
        scope_of(
            fund_name=ONECAP_PRIVATE_EQUITY,
            url=ONECAP_CANONICAL_URL,
            title=None,
            text=text,
            quote="Cilovy vynos fondu je 17 % p.a.",
            value_offset=text.index("Cilovy vynos"),
        )
        is SourceScope.EXACT_FUND
    )


def test_official_isin_is_accepted_without_the_name() -> None:
    """An ISIN of the fund settles the identity a nameless page cannot."""

    index: IsinIdentityIndex = {
        "CZ0008047511": IsinIdentity(
            fund_name=ONECAP_PRIVATE_EQUITY,
            scope=SourceScope.EXACT_FUND,
        )
    }

    text = "\n".join(
        (
            "Investicni akcie ISIN CZ0008047511",
            "Cilovy vynos je 17 % p.a.",
        )
    )

    assert (
        scope_of(
            fund_name=ONECAP_PRIVATE_EQUITY,
            url=ONECAP_CANONICAL_URL,
            text=text,
            quote="Cilovy vynos je 17 % p.a.",
            value_offset=text.index("Cilovy vynos"),
            isin_identity=index,
        )
        is SourceScope.EXACT_FUND
    )


# A fund-specific panel on a shared hub. The identity is the address the
# panel is filed under, which names this fund and no other — the AVANT fund
# row, the AMISTA `/files/<fund-slug>/` folder and the PROTON IS document
# block all have this shape.
HUB_PANEL_CASES: tuple[tuple[str, str], ...] = (
    (
        "Numero Fund SICAV, a.s.",
        "https://www.avantfunds.cz/informace-o-fondech/numero-fund-sicav-a-s/",
    ),
    (
        "Patronus třicátý sedmý SICAV a.s.",
        "https://www.amista.cz/files/patronus-tricaty-sedmy-sicav-a-s/statut.pdf",
    ),
    (
        "Nexura Multi-Asset SICAV, a.s.",
        "https://www.protonis.cz/dokumenty/nexura-multi-asset-sicav-a-s/statut.pdf",
    ),
)


def test_fund_specific_hub_panel_is_accepted() -> None:
    """
    The hub root is refused, the fund's own panel on the same hub is not.

    Each page below lists the neighbouring funds in its navigation, which is
    what the section rule is for; the address is what makes the panel this
    fund's own.
    """

    for fund_name, url in HUB_PANEL_CASES:
        text = "\n".join(
            (
                "Informace o fondech",
                "Cilovy vynos je 8 % p.a.",
            )
        )

        assert (
            scope_of(
                fund_name=fund_name,
                url=url,
                title=None,
                text=text,
                quote="Cilovy vynos je 8 % p.a.",
                value_offset=text.index("Cilovy vynos"),
            )
            is SourceScope.EXACT_FUND
        )


def test_crawl_assignment_cannot_be_fed_to_the_identity_decision() -> None:
    """
    The step-5 scope was carried into `IdentityInput` and never read.

    It names the crawl that reached a document, so on a shared host every
    fund of the house gets the same value. Leaving the channel open let a
    later reader turn it into evidence, which is the defect this batch
    found in the extraction path.
    """

    import dataclasses

    from fundscraper.document_identity import IdentityInput

    fields = {field.name for field in dataclasses.fields(IdentityInput)}

    assert "discovery_scope" not in fields


def test_document_identity_ignores_the_host_of_the_address() -> None:
    """A document proves nothing merely by sitting on a matching host."""

    from fundscraper.document_identity import (
        DocumentScope,
        IdentityInput,
        resolve_document_identity,
    )

    identity = resolve_document_identity(
        IdentityInput(
            fund_name=ONECAP_PRIVATE_EQUITY,
            url="https://www.onecap.cz/en/",
            title_text="Strategic Investments",
            body_text="We target deal sizes with an expected return of approximately 17%+ IRR.",
        )
    )

    assert identity.scope is DocumentScope.UNKNOWN
    assert not identity.is_accepted


def test_hub_panel_of_a_neighbouring_fund_is_refused() -> None:
    """The address that grants ownership must not grant it to a sibling."""

    text = "\n".join(
        (
            "Informace o fondech",
            "Cilovy vynos je 8 % p.a.",
        )
    )

    assert (
        scope_of(
            fund_name="Numero Fund SICAV, a.s.",
            url="https://www.avantfunds.cz/informace-o-fondech/patronus-tricaty-sedmy-sicav-a-s/",
            text=text,
            quote="Cilovy vynos je 8 % p.a.",
            value_offset=text.index("Cilovy vynos"),
        )
        is not SourceScope.EXACT_FUND
    )
