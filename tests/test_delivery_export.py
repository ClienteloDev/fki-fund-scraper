"""
What crosses from the auditable output into the delivered one.

The delivery file is read by colleagues, an API and a frontend. Two
things must hold for every field of it: nothing internal appears, and no
value appears that the extraction or the audit could not stand behind.
The cases here guard both, field by field, because a leak or an
overconfident value would be discovered by whoever consumes the file
rather than by us.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from fundscraper.cli import app
from fundscraper.delivery_export import (
    DELIVERY_FIELDS,
    build_delivery_records,
    delivery_summary,
    load_audit_findings,
    load_records,
)

FUND_ID = "fund_0000000000000001"


# Keys that mean something internal wherever they appear. Names that are
# reused for business data at another level — a fee tier's ``basis`` is
# the kind of tier, a news item's ``title`` is its headline — are checked
# in the tests of those structures instead.
INTERNAL_KEYS = {
    "fund_id",
    "identity",
    "raw_value",
    "scope",
    "extraction",
    "confidence",
    "review_required",
    "attempted_sources",
    "reason",
    "quote",
    "page",
    "section",
    "sha256",
    "retrieved_at",
    "processing",
    "details",
    "relation_confidence",
    "parser_name",
    "source",
}


# Text that exists only inside the internal record. None of it may be
# findable anywhere in the serialized delivery, whatever key it sat under.
INTERNAL_TEXT = (
    "Minimální investice činí 1 000 000 Kč.",
    "aaaaaaaaaaaaaaaa",
    "2026-01-15T10:00:00Z",
    "Statut fondu",
    "III.",
    "something internal",
    "https://www.examplefund.cz/other.pdf",
    "Vstupní poplatek až 3 % z výše investice",
    "raw unnormalized wording",
)


def evidence(
    *,
    url: str = "https://www.examplefund.cz/statut.pdf",
) -> dict[str, Any]:
    """Return the internal evidence shape, with everything it carries."""

    return {
        "source": {
            "url": url,
            "document_type": "statute",
            "retrieved_at": "2026-01-15T10:00:00Z",
            "title": "Statut fondu",
            "published_at": None,
            "effective_at": None,
            "sha256": "a" * 64,
        },
        "quote": "Minimální investice činí 1 000 000 Kč.",
        "page": 4,
        "section": "III.",
    }


def found(
    value: Any,
    *,
    url: str = "https://www.examplefund.cz/statut.pdf",
) -> dict[str, Any]:
    return {
        "status": "found",
        "value": value,
        "raw_value": "Minimální investice činí 1 000 000 Kč.",
        "scope": {"type": "fund", "fund_name": "Example"},
        "source": evidence(url=url),
        "extraction": {
            "method": "regex",
            "confidence": "high",
            "review_required": False,
        },
        "reason": None,
        "attempted_sources": [
            {
                "url": "https://www.examplefund.cz/other.pdf",
                "retrieved_at": "2026-01-15T10:00:00Z",
                "outcome": "conflicting_values",
            }
        ],
    }


def unresolved(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "value": None,
        "reason": {
            "code": "conflicting_values",
            "detail": "Two sources disagree.",
        },
        "attempted_sources": [],
    }


FEES_VALUE = {
    "items": [
        {
            "type": "entry",
            "rate_percent": 3.0,
            "fixed_amount": None,
            "currency": None,
            "frequency": "one_off",
            "maximum": True,
            "basis": "Vstupní poplatek až 3 % z výše investice",
            "condition": "z výše investice",
            "minimum_rate_percent": 0.0,
            "maximum_rate_percent": 3.0,
            "tiers": [],
            "negotiable": False,
            "details": "raw unnormalized wording",
        },
        {
            "type": "exit",
            "rate_percent": 3.0,
            "fixed_amount": None,
            "currency": None,
            "frequency": "conditional",
            "maximum": None,
            "basis": "Výstupní poplatek 3 % při odkupu do 3 let",
            "condition": None,
            "minimum_rate_percent": None,
            "maximum_rate_percent": None,
            "tiers": [
                {
                    "basis": "holding_period",
                    "from_months": 0,
                    "to_months": 36,
                    "from_amount": None,
                    "to_amount": None,
                    "share_class": None,
                    "distributor": None,
                    "rate_percent": 3.0,
                    "fixed_amount": None,
                    "currency": None,
                    "condition": "při odkupu do 3 let",
                },
                {
                    "basis": "holding_period",
                    "from_months": 36,
                    "to_months": None,
                    "from_amount": None,
                    "to_amount": None,
                    "share_class": None,
                    "distributor": None,
                    "rate_percent": 0.0,
                    "fixed_amount": None,
                    "currency": None,
                    "condition": "po 3 letech",
                },
            ],
            "negotiable": None,
            "details": None,
        },
    ]
}


AUM_HISTORY_VALUE = {
    "observations": [
        {
            "amount": 530_168_000.0,
            "currency": "CZK",
            "metric_type": "fund_capital",
            "as_of": "2025-12-31",
            "share_class": None,
            "scope": {"type": "fund"},
            "source": evidence(),
            "extraction": {"method": "table", "confidence": "high", "review_required": False},
        }
    ]
}


ANNUAL_RETURNS_VALUE = {
    "observations": [
        {
            "year": 2024,
            "return_percent": 6.2,
            "series_type": "calendar_year",
            "share_class": "A",
            "currency": "CZK",
            "source": evidence(),
            "extraction": {"method": "table", "confidence": "high", "review_required": False},
        }
    ]
}


HISTORICAL_VALUES_VALUE = {
    "series": [
        {
            "value_type": "nav_per_share",
            "currency": "CZK",
            "frequency": "monthly",
            "share_class": "A",
            "unit": None,
            "observations": [
                {
                    "as_of": "2025-12-31",
                    "value": 1.0739,
                    "currency": "CZK",
                    "source": evidence(),
                    "extraction": {
                        "method": "table",
                        "confidence": "high",
                        "review_required": False,
                    },
                }
            ],
        }
    ]
}


NEWS_VALUE = {
    "items": [
        {
            "title": "Nový projekt",
            "url": "https://www.examplefund.cz/aktuality/novy-projekt",
            "source_domain": "examplefund.cz",
            "source_type": "official_fund",
            "published_at": "2026-03-01",
            "summary": None,
            "relation_confidence": "medium",
        }
    ]
}


def internal_record(**overrides: Any) -> dict[str, Any]:
    """Return one internal fund record with every field found."""

    record: dict[str, Any] = {
        "fund_id": FUND_ID,
        "name": "Example Fund SICAV, a.s.",
        "web": "https://www.examplefund.cz/",
        "identity": {
            "ico": "12345678",
            "legal_name": "Example",
            "match_status": "verified",
            "confidence": "high",
        },
        "investment_horizon": found(
            {
                "recommended_years": 5.0,
                "kind": "minimum",
                "minimum_years": 5.0,
                "maximum_years": None,
                "wording": "5 let a více",
            }
        ),
        "minimum_investment": found(
            {
                "amount": 1_000_000.0,
                "currency": "CZK",
                "kind": "initial_subscription",
                "condition": None,
                "share_class": None,
                "origin": "explicit",
                "inference": None,
            }
        ),
        "target_return": found(
            {
                "value_percent_pa": 7.0,
                "minimum_percent_pa": None,
                "maximum_percent_pa": None,
                "condition": None,
                "return_type": "target",
                "annualization": "per_annum",
                "period": None,
                "share_class": None,
                "subfund": None,
            }
        ),
        "fees": found(FEES_VALUE),
        "assets_under_management": found(
            {
                "amount": 530_168_000.0,
                "currency": "CZK",
                "metric_type": "fund_capital",
                "as_of": "2025-12-31",
            }
        ),
        "manager": found(
            {
                "role": "manager",
                "name": "AVANT investiční společnost, a.s.",
                "legal_name": None,
                "ico": "27590241",
                "web": None,
            }
        ),
        "administrator": found(
            {
                "role": "administrator",
                "name": "AVANT investiční společnost, a.s.",
                "legal_name": None,
                "ico": "27590241",
                "web": None,
            }
        ),
        "aum_history": found(AUM_HISTORY_VALUE),
        "annual_returns": found(ANNUAL_RETURNS_VALUE),
        "historical_values": found(HISTORICAL_VALUES_VALUE),
        "news": found(NEWS_VALUE),
        "processing": {
            "status": "completed",
            "updated_at": "2026-01-15T10:00:00Z",
            "warnings": ["something internal"],
        },
    }

    record.update(overrides)

    return record


def deliver(
    record: dict[str, Any],
    *,
    audit_findings: list[dict[str, Any]] | None = None,
    include_source_url: bool = True,
) -> dict[str, Any]:
    return build_delivery_records(
        records=[record],
        audit_findings=audit_findings or [],
        include_source_url=include_source_url,
    )[0]


def every_key(payload: Any) -> set[str]:
    """Return every key appearing anywhere inside a structure."""

    found: set[str] = set()

    if isinstance(payload, dict):
        found |= set(payload)

        for value in payload.values():
            found |= every_key(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= every_key(item)

    return found


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_delivery_record_carries_only_the_business_fields() -> None:
    delivered = deliver(internal_record())

    assert set(delivered) == {"name", "web", *DELIVERY_FIELDS}

    assert delivered["name"] == "Example Fund SICAV, a.s."

    assert delivered["web"] == "https://www.examplefund.cz/"


def test_a_found_field_is_a_status_a_value_and_a_source_url() -> None:
    delivered = deliver(internal_record())

    horizon = delivered["investment_horizon"]

    assert set(horizon) == {"status", "value", "source_url"}

    assert horizon["status"] == "found"

    assert horizon["value"]["recommended_years"] == 5.0

    assert horizon["source_url"] == "https://www.examplefund.cz/statut.pdf"


def test_the_source_url_can_be_left_out_entirely() -> None:
    delivered = deliver(
        internal_record(),
        include_source_url=False,
    )

    assert set(delivered["investment_horizon"]) == {"status", "value"}


def test_a_missing_field_is_a_null_value() -> None:
    delivered = deliver(
        internal_record(
            minimum_investment={
                "status": "not_found",
                "value": None,
                "reason": {"code": "not_quantified", "detail": "nothing found"},
                "attempted_sources": [],
            }
        )
    )

    assert delivered["minimum_investment"] == {
        "status": "not_found",
        "value": None,
    }


# ---------------------------------------------------------------------------
# Nothing internal leaks
# ---------------------------------------------------------------------------


def test_no_internal_field_reaches_the_delivery() -> None:
    """
    The whole point of the second file is what it does not carry.

    Checked over the entire structure rather than the top level, because
    the internal evidence hangs off every observation of every series.
    """

    delivered = deliver(internal_record())

    leaked = every_key(delivered) & INTERNAL_KEYS

    assert leaked == set(), f"internal keys reached the delivery: {sorted(leaked)}"

    # Names alone are not enough: the quoted sentence, the digest, the
    # retrieval timestamp and the refused attempt must not appear under
    # any key at all.
    serialized = json.dumps(delivered, ensure_ascii=False)

    for text in INTERNAL_TEXT:
        assert text not in serialized, f"internal text reached the delivery: {text}"


def test_the_delivery_is_plain_json() -> None:
    """Whatever a frontend does with it, it has to survive a round trip."""

    delivered = deliver(internal_record())

    assert json.loads(json.dumps(delivered, ensure_ascii=False)) == delivered


# ---------------------------------------------------------------------------
# Unresolved extraction states
# ---------------------------------------------------------------------------


def test_an_unresolved_extraction_state_becomes_an_absence() -> None:
    """Ambiguous, conflicting, error and pending are not deliverable."""

    for status in (
        "ambiguous",
        "conflicting",
        "error",
        "pending",
    ):
        delivered = deliver(internal_record(target_return=unresolved(status)))

        assert delivered["target_return"] == {
            "status": "not_found",
            "value": None,
        }, status


def test_a_conflicting_field_never_carries_a_value_from_its_attempts() -> None:
    conflicting = unresolved("conflicting")

    conflicting["attempted_sources"] = [
        {
            "url": "https://www.examplefund.cz/a.pdf",
            "retrieved_at": "2026-01-15T10:00:00Z",
            "outcome": "conflicting_values",
            "detail": "8 % p.a.",
        }
    ]

    delivered = deliver(internal_record(fees=conflicting))

    assert delivered["fees"]["value"] is None

    assert "8 %" not in json.dumps(delivered, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Audit-aware export
# ---------------------------------------------------------------------------


def audit_finding(
    *,
    field: str,
    status: str,
) -> dict[str, Any]:
    return {
        "fund_id": FUND_ID,
        "fund_name": "Example Fund SICAV, a.s.",
        "field": field,
        "status": status,
        "reason_code": "implausibly_small_aum",
    }


def test_a_doubted_field_is_withheld_when_an_audit_is_supplied() -> None:
    for status in (
        "suspicious",
        "conflicting",
        "rejected",
        "missing",
    ):
        delivered = deliver(
            internal_record(),
            audit_findings=[
                audit_finding(
                    field="assets_under_management",
                    status=status,
                )
            ],
        )

        assert delivered["assets_under_management"] == {
            "status": "not_found",
            "value": None,
        }, status

        # Only the doubted field is withheld.
        assert delivered["investment_horizon"]["status"] == "found"


def test_without_an_audit_the_extraction_status_decides() -> None:
    delivered = deliver(internal_record())

    assert delivered["assets_under_management"]["status"] == "found"


def test_an_audit_finding_for_another_fund_withholds_nothing() -> None:
    other = audit_finding(
        field="assets_under_management",
        status="rejected",
    )

    other["fund_id"] = "fund_0000000000000002"

    delivered = deliver(
        internal_record(),
        audit_findings=[other],
    )

    assert delivered["assets_under_management"]["status"] == "found"


# ---------------------------------------------------------------------------
# Structures a frontend needs
# ---------------------------------------------------------------------------


def test_fee_items_and_tiers_survive_with_their_structure() -> None:
    fees = deliver(internal_record())["fees"]["value"]

    entry, exit_fee = fees["items"]

    assert entry["type"] == "entry"

    assert entry["rate_percent"] == 3.0

    assert entry["maximum"] is True

    # A range keeps both of its bounds.
    assert entry["minimum_rate_percent"] == 0.0

    assert entry["maximum_rate_percent"] == 3.0

    # The tiers of a holding-period fee stay separate rows.
    assert exit_fee["type"] == "exit"

    assert [tier["to_months"] for tier in exit_fee["tiers"]] == [36, None]

    assert [tier["rate_percent"] for tier in exit_fee["tiers"]] == [3.0, 0.0]

    assert exit_fee["tiers"][0]["basis"] == "holding_period"

    # The source line the rate was read from does not travel.
    assert "basis" not in entry

    assert "details" not in entry


def test_the_dated_series_stay_plottable() -> None:
    """A chart needs the observations, not a sentence about them."""

    delivered = deliver(internal_record())

    capital = delivered["aum_history"]["value"]["observations"][0]

    assert capital == {
        "amount": 530_168_000.0,
        "currency": "CZK",
        "metric_type": "fund_capital",
        "as_of": "2025-12-31",
        "share_class": None,
    }

    returns = delivered["annual_returns"]["value"]["observations"][0]

    assert returns == {
        "year": 2024,
        "return_percent": 6.2,
        "series_type": "calendar_year",
        "share_class": "A",
        "currency": "CZK",
    }

    series = delivered["historical_values"]["value"]["series"][0]

    assert series["value_type"] == "nav_per_share"

    assert series["frequency"] == "monthly"

    assert series["observations"] == [
        {
            "as_of": "2025-12-31",
            "value": 1.0739,
            "currency": "CZK",
        }
    ]


def test_the_parties_and_the_news_keep_what_identifies_them() -> None:
    delivered = deliver(internal_record())

    assert delivered["manager"]["value"]["name"] == "AVANT investiční společnost, a.s."

    assert delivered["manager"]["value"]["ico"] == "27590241"

    item = delivered["news"]["value"]["items"][0]

    assert item["title"] == "Nový projekt"

    assert item["published_at"] == "2026-03-01"

    # The confidence that the item belongs to this fund is internal.
    assert "relation_confidence" not in item


def test_a_series_with_no_usable_observation_is_an_absence() -> None:
    delivered = deliver(
        internal_record(
            annual_returns={
                "status": "found",
                "value": {"observations": []},
                "raw_value": "x",
                "source": evidence(),
                "extraction": {
                    "method": "table",
                    "confidence": "low",
                    "review_required": True,
                },
            }
        )
    )

    assert delivered["annual_returns"] == {
        "status": "not_found",
        "value": None,
    }


# ---------------------------------------------------------------------------
# Files and the command
# ---------------------------------------------------------------------------


def test_the_summary_counts_delivered_values() -> None:
    counts = delivery_summary([deliver(internal_record())])

    assert counts["investment_horizon"] == 1

    assert set(counts) == set(DELIVERY_FIELDS)


def test_the_command_writes_the_delivery_file(
    tmp_path: Path,
) -> None:
    internal = tmp_path / "funds.full.json"

    internal.write_text(
        json.dumps([internal_record()], ensure_ascii=False),
        encoding="utf-8",
    )

    output = tmp_path / "funds.delivery.json"

    result = CliRunner().invoke(
        app,
        [
            "export-delivery",
            "--input",
            str(internal),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.stdout

    delivered = json.loads(output.read_text(encoding="utf-8"))

    assert len(delivered) == 1

    assert set(delivered[0]) == {"name", "web", *DELIVERY_FIELDS}

    # The internal file is only read.
    assert json.loads(internal.read_text(encoding="utf-8")) == [internal_record()]


def test_the_command_refuses_to_overwrite_the_internal_output(
    tmp_path: Path,
) -> None:
    internal = tmp_path / "funds.full.json"

    internal.write_text(
        json.dumps([internal_record()], ensure_ascii=False),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "export-delivery",
            "--input",
            str(internal),
            "--output",
            str(internal),
        ],
    )

    assert result.exit_code == 1

    assert json.loads(internal.read_text(encoding="utf-8")) == [internal_record()]


def test_the_command_applies_an_audit_report(
    tmp_path: Path,
) -> None:
    internal = tmp_path / "funds.full.json"

    internal.write_text(
        json.dumps([internal_record()], ensure_ascii=False),
        encoding="utf-8",
    )

    audit = tmp_path / "audit.json"

    audit.write_text(
        json.dumps(
            {
                "findings": [
                    audit_finding(
                        field="target_return",
                        status="suspicious",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "funds.delivery.json"

    result = CliRunner().invoke(
        app,
        [
            "export-delivery",
            "--input",
            str(internal),
            "--output",
            str(output),
            "--audit",
            str(audit),
        ],
    )

    assert result.exit_code == 0, result.stdout

    delivered = json.loads(output.read_text(encoding="utf-8"))[0]

    assert delivered["target_return"] == {
        "status": "not_found",
        "value": None,
    }

    assert delivered["investment_horizon"]["status"] == "found"


def test_loading_helpers_reject_what_they_cannot_read(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.json"

    broken.write_text("{not json", encoding="utf-8")

    try:
        load_records(broken)
    except Exception as exc:
        assert "Invalid JSON" in str(exc)
    else:
        raise AssertionError("invalid JSON should be refused")

    assert load_audit_findings(None) == []
