from __future__ import annotations

from pathlib import Path
from typing import Any

from fundscraper.output_audit import (
    ALL_AUDITED_FIELDS,
    AUDITED_FIELDS,
    AuditStatus,
    audit_enriched_output,
    audit_field,
)

FUND_NAME = "Rezidento Alfa SICAV, a.s."

FUND_WEB = "https://www.rezidentoalfa.cz"

SOURCE = {
    "source": {
        "url": "https://www.rezidentoalfa.cz/vyrocni-zprava.pdf",
        "document_type": "annual_report",
        "retrieved_at": "2026-07-23T12:00:00+00:00",
    },
    "quote": "Fondový kapitál k 31. 12. 2024",
    "page": 7,
}


def _field(value: Any) -> dict[str, Any]:
    return {
        "status": "found",
        "value": value,
        "raw_value": "text",
        "source": SOURCE,
        "extraction": {
            "method": "table",
            "confidence": "high",
            "review_required": False,
        },
    }


def _audit(
    *,
    field: str,
    value: Any,
) -> list[str]:
    outcome = audit_field(
        fund_id="fund_0123456789abcdef",
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        field=field,
        payload=_field(value),
        shared_sources={},
    )

    return [finding.reason_code for finding in outcome.findings]


def test_reports_a_party_stored_as_a_bare_legal_form() -> None:
    codes = _audit(
        field="manager",
        value={
            "role": "manager",
            "name": "Investiční společnost",
        },
    )

    assert "party_name_not_specific" in codes


def test_reports_a_party_that_repeats_the_name_of_the_fund() -> None:
    codes = _audit(
        field="manager",
        value={
            "role": "manager",
            "name": "Rezidento Alfa SICAV, a.s.",
        },
    )

    assert "party_is_the_fund_itself" in codes


def test_reports_a_party_stored_under_the_wrong_role() -> None:
    codes = _audit(
        field="administrator",
        value={
            "role": "manager",
            "name": "CODYA investiční společnost, a.s.",
        },
    )

    assert "party_role_mismatch" in codes


def test_accepts_a_properly_named_manager() -> None:
    outcome = audit_field(
        fund_id="fund_0123456789abcdef",
        fund_name=FUND_NAME,
        fund_web=FUND_WEB,
        field="manager",
        payload=_field(
            {
                "role": "manager",
                "name": "CODYA investiční společnost, a.s.",
                "ico": "06876897",
            }
        ),
        shared_sources={},
    )

    assert outcome.status is AuditStatus.VALID


def test_reports_a_registered_capital_delivered_as_fund_assets() -> None:
    codes = _audit(
        field="aum_history",
        value={
            "observations": [
                {
                    "amount": 100000.0,
                    "currency": "CZK",
                    "metric_type": "registered_capital",
                    "as_of": "2024-12-31",
                }
            ]
        },
    )

    assert "statutory_capital_as_aum" in codes


def test_reports_a_kid_scenario_delivered_as_an_annual_return() -> None:
    codes = _audit(
        field="annual_returns",
        value={
            "observations": [
                {
                    "year": 2029,
                    "return_percent": 4.8,
                    "series_type": "kid_scenario",
                }
            ]
        },
    )

    assert "kid_scenario_as_annual_return" in codes


def test_reports_conflicting_results_for_one_year() -> None:
    codes = _audit(
        field="annual_returns",
        value={
            "observations": [
                {
                    "year": 2024,
                    "return_percent": 6.5,
                },
                {
                    "year": 2024,
                    "return_percent": 8.1,
                },
            ]
        },
    )

    assert "conflicting_annual_returns" in codes


def test_reports_a_news_item_of_another_fund() -> None:
    codes = _audit(
        field="news",
        value={
            "items": [
                {
                    "title": "Výsledky fondu Beta za rok 2024",
                    "url": "https://www.spravce.cz/aktuality/beta-2024",
                    "source_domain": "spravce.cz",
                    "source_type": "manager",
                    "relation_confidence": "low",
                }
            ]
        },
    )

    assert "news_of_another_fund" in codes


def test_reports_a_fee_range_collapsed_to_one_number() -> None:
    codes = _audit(
        field="fees",
        value={
            "items": [
                {
                    "type": "entry",
                    "rate_percent": 6.0,
                    "basis": "Vstupní poplatek od 0 % do 6 % z výše investice",
                }
            ]
        },
    )

    assert "collapsed_fee_range" in codes


def test_reports_an_inferred_minimum_without_a_legal_basis() -> None:
    codes = _audit(
        field="minimum_investment",
        value={
            "amount": 1_000_000.0,
            "currency": "CZK",
            "kind": "legal_threshold",
            "origin": "inferred",
        },
    )

    assert "inferred_minimum_without_basis" in codes


def test_a_file_without_the_new_fields_is_not_reported_as_missing_them(
    tmp_path: Path,
) -> None:
    record: dict[str, Any] = {
        "name": FUND_NAME,
        "web": FUND_WEB,
    }

    for field in AUDITED_FIELDS:
        record[field] = {
            "status": "not_found",
            "reason": {
                "code": "source_not_found",
                "detail": "No source was available.",
            },
        }

    report = audit_enriched_output(
        records=[record],
        input_path=tmp_path / "funds.enriched.json",
        input_sha256="0" * 64,
    )

    assert report.summary.fields_checked == len(AUDITED_FIELDS)

    assert set(report.summary.by_field) == set(ALL_AUDITED_FIELDS)

    assert not report.summary.by_field["manager"]
