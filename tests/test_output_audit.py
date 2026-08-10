from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fundscraper.output_audit import (
    AuditStatus,
    SourceType,
    audit_enriched_output,
    audit_field,
    classify_source,
    file_digest,
    load_enriched_records,
    write_audit_report,
)

FUND_WEB = "https://www.examplefond.cz/"


def field_payload(
    value: Any,
    *,
    url: str = "https://www.examplefond.cz/statut.pdf",
    retrieved_at: str = "2026-01-15T10:00:00Z",
) -> dict[str, Any]:
    return {
        "status": "found",
        "value": value,
        "source": {
            "url": url,
            "retrieved_at": retrieved_at,
        },
    }


def audit_one(
    field: str,
    payload: Any,
    *,
    fund_name: str = "EXAMPLE fond SICAV, a.s.",
    shared: dict[str, set[str]] | None = None,
) -> Any:
    return audit_field(
        fund_id="fund_0000000000000000",
        fund_name=fund_name,
        fund_web=FUND_WEB,
        field=field,
        payload=payload,
        shared_sources=shared or {},
    )


def reason_codes(outcome: Any) -> set[str]:
    return {finding.reason_code for finding in outcome.findings}


def test_not_found_field_is_classified_as_missing() -> None:
    outcome = audit_one(
        "minimum_investment",
        {
            "status": "not_found",
            "reason": {
                "code": "not_quantified",
                "detail": "nothing found",
            },
        },
    )

    assert outcome.status is AuditStatus.MISSING
    assert outcome.findings == ()


def test_plausible_values_are_valid() -> None:
    assert (
        audit_one(
            "minimum_investment",
            field_payload(
                {
                    "amount": 1_000_000.0,
                    "currency": "CZK",
                    "kind": "initial_subscription",
                    "condition": None,
                }
            ),
        ).status
        is AuditStatus.VALID
    )

    assert (
        audit_one(
            "investment_horizon",
            field_payload({"recommended_years": 5.0}),
        ).status
        is AuditStatus.VALID
    )

    assert (
        audit_one(
            "target_return",
            field_payload(
                {
                    "value_percent_pa": 7.0,
                    "minimum_percent_pa": None,
                    "maximum_percent_pa": None,
                    "condition": None,
                }
            ),
        ).status
        is AuditStatus.VALID
    )


def test_detects_implausibly_small_minimum_investment() -> None:
    outcome = audit_one(
        "minimum_investment",
        field_payload(
            {
                "amount": 1.0,
                "currency": "EUR",
                "kind": "initial_subscription",
                "condition": None,
            }
        ),
    )

    assert outcome.status is AuditStatus.SUSPICIOUS
    assert "implausibly_small_minimum_investment" in reason_codes(outcome)


def test_detects_zero_and_non_round_minimum_investment() -> None:
    zero = audit_one(
        "minimum_investment",
        field_payload(
            {
                "amount": 0.0,
                "currency": "CZK",
                "kind": "initial_subscription",
                "condition": None,
            }
        ),
    )

    assert "zero_minimum_investment" in reason_codes(zero)

    unit_price = audit_one(
        "minimum_investment",
        field_payload(
            {
                "amount": 1.1138,
                "currency": "CZK",
                "kind": "initial_subscription",
                "condition": None,
            }
        ),
    )

    assert "non_round_minimum_investment" in reason_codes(unit_price)


def test_detects_year_captured_as_target_return() -> None:
    outcome = audit_one(
        "target_return",
        field_payload(
            {
                "value_percent_pa": 2026.0,
                "minimum_percent_pa": None,
                "maximum_percent_pa": None,
                "condition": None,
            }
        ),
    )

    # A calendar year is not an unusual return, it is not a return at
    # all, so the value is refused rather than only doubted.
    assert outcome.status is AuditStatus.REJECTED
    assert "year_captured_as_percentage" in reason_codes(outcome)


def test_detects_kid_performance_scenario_as_target_return() -> None:
    outcome = audit_one(
        "target_return",
        field_payload(
            {
                "value_percent_pa": 41.3,
                "minimum_percent_pa": None,
                "maximum_percent_pa": None,
                "condition": None,
            },
            url="https://www.avantfunds.cz/wp-content/uploads/kid_example_2025.pdf",
        ),
    )

    assert "kid_performance_scenario_as_target" in reason_codes(outcome)


def test_inverted_target_return_range_is_conflicting() -> None:
    outcome = audit_one(
        "target_return",
        field_payload(
            {
                "value_percent_pa": None,
                "minimum_percent_pa": 12.0,
                "maximum_percent_pa": 6.0,
                "condition": None,
            }
        ),
    )

    assert outcome.status is AuditStatus.CONFLICTING
    assert "inverted_target_return_range" in reason_codes(outcome)


def test_detects_lost_thousands_multiplier_in_aum() -> None:
    outcome = audit_one(
        "assets_under_management",
        field_payload(
            {
                "amount": 12_202.0,
                "currency": "CZK",
                "metric_type": "net_assets",
                "as_of": "2025-12-31",
            },
            url="https://example.cz/vz_2025_example.pdf",
        ),
    )

    assert outcome.status is AuditStatus.SUSPICIOUS
    assert "thousands_unit_not_applied" in reason_codes(outcome)


def test_detects_missing_aum_date_and_stale_date() -> None:
    undated = audit_one(
        "assets_under_management",
        field_payload(
            {
                "amount": 500_000_000.0,
                "currency": "CZK",
                "metric_type": "net_assets",
                "as_of": None,
            }
        ),
    )

    assert "missing_as_of_date" in reason_codes(undated)

    stale = audit_one(
        "assets_under_management",
        field_payload(
            {
                "amount": 500_000_000.0,
                "currency": "CZK",
                "metric_type": "net_assets",
                "as_of": "2017-11-07",
            }
        ),
    )

    assert "stale_as_of_date" in reason_codes(stale)


def test_zero_fee_taken_from_a_range_is_flagged() -> None:
    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "entry",
                        "rate_percent": 0.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "one_off",
                        "maximum": False,
                        "basis": "od 0 % do 6 % z objemu investice dle Smlouvy o investici",
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert "zero_fee_from_a_range" in reason_codes(outcome)


def test_zero_fee_supported_by_the_source_is_valid() -> None:
    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "entry",
                        "rate_percent": 0.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "one_off",
                        "maximum": False,
                        "basis": "Vstupni poplatek (prirazka) 0 %",
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert outcome.status is AuditStatus.VALID


def test_genuine_high_exit_and_performance_fees_are_not_flagged() -> None:
    """A 95 % exit fee and a 45 % performance fee are real Czech fund terms."""

    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "exit",
                        "rate_percent": 95.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "one_off",
                        "maximum": False,
                        "basis": "Vystupni poplatek 95 % behem investicni periody",
                        "condition": None,
                    },
                    {
                        "type": "performance",
                        "rate_percent": 45.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "conditional",
                        "maximum": False,
                        "basis": "Vykonnostni odmena: 45 % z vynosu nad 8 % p.a., High-Water Mark",
                        "condition": None,
                    },
                ]
            }
        ),
    )

    assert "unusually_high_fee_rate" not in reason_codes(outcome)


def test_income_share_sentence_is_flagged_only_when_detached_from_the_label() -> None:
    detached = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "entry",
                        "rate_percent": 100.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "one_off",
                        "maximum": False,
                        "basis": (
                            "Vstupni poplatek (prirazka) je prijmem Spolecnosti. "
                            "SNT ve tride A je 100 % tzv. Baze_A."
                        ),
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert "percentage_describes_income_share" in reason_codes(detached)

    attached = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "performance",
                        "rate_percent": 20.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "conditional",
                        "maximum": False,
                        "basis": ("Vykonnostni odmena cini 20 % z vykonnosti Podfondu"),
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert "percentage_describes_income_share" not in reason_codes(attached)


def test_maximum_wording_without_the_maximum_flag_is_conflicting() -> None:
    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "exit",
                        "rate_percent": 40.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "one_off",
                        "maximum": False,
                        "basis": "Vystupni poplatek max. 40 % z aktualni hodnoty",
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert outcome.status is AuditStatus.CONFLICTING
    assert "maximum_flag_contradicts_source" in reason_codes(outcome)


def test_source_shared_by_several_funds_is_conflicting() -> None:
    url = "https://www.avantfunds.cz/fondy/4-gimel-investments-sicav-a-s/podfond-alfa/"

    outcome = audit_one(
        "investment_horizon",
        field_payload(
            {"recommended_years": 5.0},
            url=url,
        ),
        shared={
            url: {
                "EXAMPLE fond SICAV, a.s.",
                "TOLAR SICAV a. s.",
                "ARBITAS SICAV, a.s.",
            }
        },
    )

    assert outcome.status is AuditStatus.CONFLICTING

    finding = next(
        item for item in outcome.findings if item.reason_code == "source_shared_across_funds"
    )

    assert finding.related_funds == [
        "ARBITAS SICAV, a.s.",
        "TOLAR SICAV a. s.",
    ]


def test_manager_source_without_the_fund_name_is_suspicious() -> None:
    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "management",
                        "rate_percent": 2.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "annual",
                        "maximum": False,
                        "basis": "Poplatek za obhospodarovani 2 %",
                        "condition": None,
                    }
                ]
            },
            url="https://www.avantfunds.cz/wp-content/uploads/kid_fond_2026.pdf",
        ),
    )

    assert "source_does_not_name_the_fund" in reason_codes(outcome)


def test_classifies_source_types() -> None:
    assert (
        classify_source(
            source_url="https://www.examplefond.cz/statut.pdf",
            fund_web=FUND_WEB,
        )
        is SourceType.OFFICIAL_WEBSITE
    )

    assert (
        classify_source(
            source_url="https://www.avantfunds.cz/fondy/example/",
            fund_web=FUND_WEB,
        )
        is SourceType.MANAGER_OR_ADMINISTRATOR
    )

    assert (
        classify_source(
            source_url="https://www.emis.com/php/company-profile/CZ/Example",
            fund_web=FUND_WEB,
        )
        is SourceType.THIRD_PARTY
    )

    assert (
        classify_source(
            source_url=None,
            fund_web=FUND_WEB,
        )
        is SourceType.NONE
    )


def test_report_summarizes_and_never_modifies_the_input(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "funds.enriched.json"

    payload = [
        {
            "name": "EXAMPLE fond SICAV, a.s.",
            "web": FUND_WEB,
            "investment_horizon": field_payload({"recommended_years": 5.0}),
            "minimum_investment": field_payload(
                {
                    "amount": 1.0,
                    "currency": "EUR",
                    "kind": "initial_subscription",
                    "condition": None,
                }
            ),
            "target_return": {
                "status": "not_found",
                "reason": {
                    "code": "not_quantified",
                    "detail": "nothing",
                },
            },
            "fees": {
                "status": "not_found",
                "reason": {
                    "code": "not_quantified",
                    "detail": "nothing",
                },
            },
            "assets_under_management": {
                "status": "not_found",
                "reason": {
                    "code": "not_quantified",
                    "detail": "nothing",
                },
            },
        }
    ]

    input_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    digest_before = file_digest(input_path)

    records = load_enriched_records(input_path)

    report = audit_enriched_output(
        records=records,
        input_path=input_path,
        input_sha256=digest_before,
    )

    report_path = tmp_path / "audit.json"

    write_audit_report(
        report=report,
        path=report_path,
    )

    assert file_digest(input_path) == digest_before

    assert report.summary.funds == 1
    assert report.summary.fields_checked == 5
    assert report.summary.by_status["missing"] == 3
    assert report.summary.by_status["valid"] == 1
    assert report.summary.by_status["suspicious"] == 1

    assert report.summary.by_field["minimum_investment"]["suspicious"] == 1

    assert report.schema_notes

    written = json.loads(report_path.read_text(encoding="utf-8"))

    assert written["input_sha256"] == digest_before
    assert written["findings"][0]["field"] == "minimum_investment"


def test_range_maximum_split_by_column_interleaving_is_not_flagged() -> None:
    """From the RESIDENTO statute: "od 0 % do 3 %" split across columns."""

    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "entry",
                        "rate_percent": 3.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "one_off",
                        "maximum": True,
                        "basis": (
                            "do 3 % z vyse investice, a to dle prislusne "
                            "Smlouvy o investici. Vstupni poplatek je"
                        ),
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert "value_far_from_fee_label" not in reason_codes(outcome)


def test_income_share_far_from_its_label_is_still_flagged() -> None:
    """The FWG profit split must stay reported even after the range fix."""

    outcome = audit_one(
        "fees",
        field_payload(
            {
                "items": [
                    {
                        "type": "performance",
                        "rate_percent": 75.0,
                        "fixed_amount": None,
                        "currency": None,
                        "frequency": "conditional",
                        "maximum": False,
                        "basis": (
                            "o na PRIA nalezi pouze 75 % z tohoto jejich vynosu nad "
                            "7 % p.a. a zbyvajicich 25 % se (jako performance fee) pripise"
                        ),
                        "condition": None,
                    }
                ]
            }
        ),
    )

    assert reason_codes(outcome) & {
        "value_far_from_fee_label",
        "percentage_describes_income_share",
    }


def test_assets_with_a_unit_in_the_evidence_are_a_selection_problem() -> None:
    outcome = audit_one(
        "assets_under_management",
        field_payload(
            {
                "amount": 17_000.0,
                "currency": "CZK",
                "metric_type": "net_assets",
                "as_of": "2025-12-31",
            },
            url="https://example.cz/vz_2025.pdf",
        ),
    )

    assert "thousands_unit_not_applied" in reason_codes(outcome)
