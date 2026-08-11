"""
Which funds earn a second crawl, and what the second crawl looks for.

The decisions here are the whole point of the two-pass run: crawling
every fund as deeply as the hardest one costs a great deal and buys
almost nothing, and crawling a fund again for a problem no crawl can fix
costs the same and buys nothing at all.
"""

from __future__ import annotations

from typing import Any

from fundscraper.crawl_planning import (
    DEEP_BUDGET,
    FAST_BUDGET,
    FIELD_DOCUMENT_TYPES,
    TRIGGER_FIELDS,
    DeepPassTrigger,
    conflict_is_between_sources,
    select_authoritative_documents,
    select_deep_pass_funds,
)
from fundscraper.html_discovery import DiscoveredLink
from fundscraper.output_models import DocumentType

FUND_ID = "fund_0000000000000001"


def output_record(
    **fields: str,
) -> dict[str, Any]:
    """Return one delivered fund with the given field statuses."""

    record: dict[str, Any] = {
        "fund_id": FUND_ID,
        "name": "Example Fund SICAV a.s.",
    }

    for name in TRIGGER_FIELDS:
        record[name] = {"status": fields.get(name, "found")}

    record["news"] = {"status": fields.get("news", "not_found")}

    return record


def document(
    *,
    url: str,
    text: str = "",
    score: int = 100,
    document_type: DocumentType = DocumentType.STATUTE,
) -> DiscoveredLink:
    return DiscoveredLink(
        url=url,
        text=text,
        score=score,
        document_type=document_type,
        same_domain=True,
        direct_document=True,
    )


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_the_fast_pass_is_cheaper_than_the_deep_one_in_every_dimension() -> None:
    assert FAST_BUDGET.official_max_pages < DEEP_BUDGET.official_max_pages
    assert FAST_BUDGET.official_max_documents < DEEP_BUDGET.official_max_documents
    assert FAST_BUDGET.max_pages < DEEP_BUDGET.max_pages
    assert FAST_BUDGET.max_depth < DEEP_BUDGET.max_depth
    assert FAST_BUDGET.max_documents < DEEP_BUDGET.max_documents


def test_only_the_fast_pass_stops_as_soon_as_it_has_enough() -> None:
    assert FAST_BUDGET.stop_when_sufficient is True

    # The deep pass exists precisely to look past the obvious documents.
    assert DEEP_BUDGET.stop_when_sufficient is False


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_a_fund_whose_fields_are_all_answered_is_not_crawled_again() -> None:
    assert select_deep_pass_funds(output_records=[output_record()]) == []


def test_a_missing_field_sends_its_fund_into_the_deep_pass() -> None:
    plans = select_deep_pass_funds(
        output_records=[output_record(assets_under_management="not_found")]
    )

    assert len(plans) == 1

    assert plans[0].fields == ("assets_under_management",)

    assert plans[0].reasons[0].trigger is DeepPassTrigger.FIELD_UNANSWERED


def test_missing_news_alone_never_sends_a_fund_into_the_deep_pass() -> None:
    """An announcement page nobody publishes is not a reason to crawl."""

    assert select_deep_pass_funds(output_records=[output_record(news="not_found")]) == []


def test_a_rejected_value_sends_its_fund_back_out() -> None:
    plans = select_deep_pass_funds(
        output_records=[output_record()],
        audit_findings=[
            {
                "fund_id": FUND_ID,
                "fund_name": "Example Fund SICAV a.s.",
                "field": "target_return",
                "status": "rejected",
                "reason_code": "unrelated_percentage_as_target_return",
            }
        ],
    )

    assert len(plans) == 1

    assert plans[0].reasons[0].trigger is DeepPassTrigger.AUDIT_REJECTED


def test_a_suspicious_value_only_counts_when_a_better_source_could_fix_it() -> None:
    """
    A number read wrongly is not a crawling problem.

    "The amount is implausibly small" says the value was misread; "the
    source does not name the fund" says the wrong document was used, and
    only the second one is worth another crawl.
    """

    misread = select_deep_pass_funds(
        output_records=[output_record()],
        audit_findings=[
            {
                "fund_id": FUND_ID,
                "field": "assets_under_management",
                "status": "suspicious",
                "reason_code": "implausibly_small_aum",
            }
        ],
    )

    assert misread == []

    wrong_document = select_deep_pass_funds(
        output_records=[output_record()],
        audit_findings=[
            {
                "fund_id": FUND_ID,
                "field": "assets_under_management",
                "status": "suspicious",
                "reason_code": "source_does_not_name_the_fund",
            }
        ],
    )

    assert len(wrong_document) == 1

    assert wrong_document[0].reasons[0].trigger is DeepPassTrigger.SOURCE_FIXABLE_FINDING


def test_a_conflict_between_two_sources_sends_the_fund_back_out() -> None:
    plans = select_deep_pass_funds(
        output_records=[output_record()],
        conflict_records=[
            {
                "fund_name": "Example Fund SICAV a.s.",
                "field": "fees",
                "outcome": "unresolved",
                "semantic_key": "fees",
                "alternatives": [
                    {"source_url": "https://www.examplefund.cz/statut.pdf"},
                    {"source_url": "https://www.examplefund.cz/cenik.pdf"},
                ],
            }
        ],
    )

    assert len(plans) == 1

    assert plans[0].reasons[0].trigger is DeepPassTrigger.UNRESOLVED_BETWEEN_SOURCES


def test_one_document_read_twice_never_sends_a_fund_back_out() -> None:
    """
    Step 8 found half of all unresolved conflicts inside one document.

    Crawling wider returns the same page and the same two readings of
    it, so those funds are deliberately left alone.
    """

    conflict = {
        "fund_name": "Example Fund SICAV a.s.",
        "field": "aum_history",
        "outcome": "unresolved",
        "semantic_key": "net_assets|2022-06-30||CZK",
        "alternatives": [
            {"source_url": "https://www.examplefund.cz/zprava.pdf"},
            {"source_url": "https://www.examplefund.cz/zprava.pdf"},
        ],
    }

    assert conflict_is_between_sources(conflict) is False

    assert (
        select_deep_pass_funds(
            output_records=[output_record()],
            conflict_records=[conflict],
        )
        == []
    )


def test_a_resolved_conflict_is_not_a_reason_to_crawl_again() -> None:
    assert (
        select_deep_pass_funds(
            output_records=[output_record()],
            conflict_records=[
                {
                    "fund_name": "Example Fund SICAV a.s.",
                    "field": "fees",
                    "outcome": "auto_resolved",
                    "alternatives": [
                        {"source_url": "https://www.examplefund.cz/a.pdf"},
                        {"source_url": "https://www.examplefund.cz/b.pdf"},
                    ],
                }
            ],
        )
        == []
    )


# ---------------------------------------------------------------------------
# Field awareness
# ---------------------------------------------------------------------------


def test_the_deep_pass_looks_for_the_documents_that_answer_the_field() -> None:
    assets = select_deep_pass_funds(
        output_records=[output_record(assets_under_management="not_found")]
    )[0]

    assert DocumentType.ANNUAL_REPORT in assets.wanted_document_types
    assert DocumentType.FINANCIAL_STATEMENTS in assets.wanted_document_types

    # The key information document states terms, not results.
    assert DocumentType.PRIIPS_KID not in assets.wanted_document_types

    terms = select_deep_pass_funds(output_records=[output_record(minimum_investment="not_found")])[
        0
    ]

    assert DocumentType.PRIIPS_KID in terms.wanted_document_types
    assert DocumentType.STATUTE in terms.wanted_document_types
    assert DocumentType.ANNUAL_REPORT not in terms.wanted_document_types


def test_every_field_that_can_trigger_a_deep_pass_knows_what_to_look_for() -> None:
    assert set(FIELD_DOCUMENT_TYPES) == set(TRIGGER_FIELDS)

    for wanted in FIELD_DOCUMENT_TYPES.values():
        assert wanted


# ---------------------------------------------------------------------------
# One authoritative copy
# ---------------------------------------------------------------------------


def test_keeps_the_newest_revision_of_a_statute_and_drops_the_older_one() -> None:
    selection = select_authoritative_documents(
        [
            document(
                url="https://www.examplefund.cz/statut-2019.pdf",
                text="Statut 2019",
            ),
            document(
                url="https://www.examplefund.cz/statut-2026.pdf",
                text="Statut 2026",
            ),
        ]
    )

    assert [item.url for item in selection.kept] == ["https://www.examplefund.cz/statut-2026.pdf"]

    assert len(selection.superseded) == 1


def test_keeps_every_year_of_an_annual_report() -> None:
    """A report of 2022 and one of 2024 are different facts."""

    selection = select_authoritative_documents(
        [
            document(
                url="https://www.examplefund.cz/vz-2022.pdf",
                document_type=DocumentType.ANNUAL_REPORT,
            ),
            document(
                url="https://www.examplefund.cz/vz-2023.pdf",
                document_type=DocumentType.ANNUAL_REPORT,
            ),
            document(
                url="https://www.examplefund.cz/vz-2024.pdf",
                document_type=DocumentType.ANNUAL_REPORT,
            ),
        ]
    )

    assert len(selection.kept) == 3

    assert selection.superseded == ()


def test_drops_a_second_copy_of_one_report_year() -> None:
    """The same annual report on the fund site and on the manager site."""

    selection = select_authoritative_documents(
        [
            document(
                url="https://www.examplefund.cz/vz-2024.pdf",
                document_type=DocumentType.ANNUAL_REPORT,
                score=120,
            ),
            document(
                url="https://www.avantfunds.cz/fondy/example/vz-2024.pdf",
                document_type=DocumentType.ANNUAL_REPORT,
                score=90,
            ),
        ]
    )

    assert len(selection.kept) == 1

    assert selection.kept[0].url == "https://www.examplefund.cz/vz-2024.pdf"

    assert len(selection.superseded) == 1


def test_two_undated_documents_of_one_type_are_kept_apart_by_their_names() -> None:
    selection = select_authoritative_documents(
        [
            document(
                url="https://www.examplefund.cz/prehled-a.pdf",
                document_type=DocumentType.FACTSHEET,
            ),
            document(
                url="https://www.examplefund.cz/prehled-b.pdf",
                document_type=DocumentType.FACTSHEET,
            ),
        ]
    )

    assert len(selection.kept) == 2
