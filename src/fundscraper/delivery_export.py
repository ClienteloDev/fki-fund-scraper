"""
The clean output a colleague, an API or a frontend actually wants.

`funds.full.json` is the auditable record: every value carries the
document it came from, the sentence, the page, the scope, the confidence,
the sources that were tried and refused. That is what makes the data
defensible, and it is the wrong thing to hand to anyone who simply wants
the numbers.

This module derives a second file from it. Each field becomes a status
and a value, and nothing else crosses over. The rules are deliberately
one-directional: an allowlist decides what may appear, so a field added
to the internal model later cannot leak here by default.

Nothing is invented. A value that could not be delivered safely is
reported as `not_found` with a null value, never as a guess.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

# The fields the delivery carries, in the order they appear.
DELIVERY_FIELDS: Final[tuple[str, ...]] = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
    "news",
)


# The only two statuses the delivery format knows. Everything the
# extraction could not settle — ambiguous, conflicting, error, pending —
# becomes an absence rather than a value a reader might trust.
STATUS_FOUND: Final = "found"

STATUS_NOT_FOUND: Final = "not_found"


# The audit verdicts that stop a value from being delivered. A field with
# no finding at all is valid and may be delivered.
BLOCKING_AUDIT_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "suspicious",
        "conflicting",
        "rejected",
        "missing",
    }
)


class DeliveryExportError(RuntimeError):
    """Raised when the internal output cannot be converted."""


# What each value may contain. An allowlist rather than a list of things
# to strip: a field added to the internal model later stays out of the
# delivery until somebody decides it belongs here.
#
# Deliberately absent from every entry: the quoted source text, the page,
# the scope, the extraction metadata, the parser, and the free-text
# fields the extraction keeps when it cannot normalize the wording
# (``basis`` and ``details`` on a fee).
_HORIZON_KEYS: Final = (
    "recommended_years",
    "kind",
    "minimum_years",
    "maximum_years",
    "wording",
)

_INFERENCE_KEYS: Final = (
    "inference_type",
    "legal_basis",
    "jurisdiction",
    "effective_date",
)

_MINIMUM_KEYS: Final = (
    "amount",
    "currency",
    "kind",
    "condition",
    "share_class",
    "origin",
)

_TARGET_RETURN_KEYS: Final = (
    "value_percent_pa",
    "minimum_percent_pa",
    "maximum_percent_pa",
    "condition",
    "return_type",
    "annualization",
    "period",
    "share_class",
    "subfund",
)

_FEE_TIER_KEYS: Final = (
    "basis",
    "from_months",
    "to_months",
    "from_amount",
    "to_amount",
    "share_class",
    "distributor",
    "rate_percent",
    "fixed_amount",
    "currency",
    "condition",
)

_FEE_ITEM_KEYS: Final = (
    "type",
    "rate_percent",
    "fixed_amount",
    "currency",
    "frequency",
    "maximum",
    "minimum_rate_percent",
    "maximum_rate_percent",
    "negotiable",
    "condition",
)

_AUM_KEYS: Final = (
    "amount",
    "currency",
    "metric_type",
    "as_of",
)

_PARTY_KEYS: Final = (
    "role",
    "name",
    "legal_name",
    "ico",
    "web",
)

_CAPITAL_OBSERVATION_KEYS: Final = (
    "amount",
    "currency",
    "metric_type",
    "as_of",
    "share_class",
)

_ANNUAL_RETURN_KEYS: Final = (
    "year",
    "return_percent",
    "series_type",
    "share_class",
    "currency",
)

_HISTORICAL_SERIES_KEYS: Final = (
    "value_type",
    "currency",
    "frequency",
    "share_class",
    "unit",
)

_HISTORICAL_OBSERVATION_KEYS: Final = (
    "as_of",
    "value",
    "currency",
)

_NEWS_KEYS: Final = (
    "title",
    "url",
    "source_domain",
    "source_type",
    "published_at",
    "summary",
)


def _picked(
    value: Any,
    keys: Sequence[str],
) -> dict[str, Any] | None:
    """Return only the allowed keys of one object."""

    if not isinstance(value, dict):
        return None

    return {key: value.get(key) for key in keys}


def _picked_list(
    value: Any,
    key: str,
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    """Return the allowed keys of every item of a wrapped list."""

    if not isinstance(value, dict):
        return []

    items = value.get(key)

    if not isinstance(items, list):
        return []

    cleaned = [_picked(item, keys) for item in items]

    return [item for item in cleaned if item is not None]


def _clean_horizon(value: Any) -> Any:
    return _picked(value, _HORIZON_KEYS)


def _clean_minimum_investment(value: Any) -> Any:
    cleaned = _picked(value, _MINIMUM_KEYS)

    if cleaned is None:
        return None

    # The legal ground of an inferred minimum is part of what the value
    # means, so it travels with it.
    cleaned["inference"] = _picked(
        value.get("inference"),
        _INFERENCE_KEYS,
    )

    return cleaned


def _clean_target_return(value: Any) -> Any:
    return _picked(value, _TARGET_RETURN_KEYS)


def _clean_fees(value: Any) -> Any:
    """
    Keep the charges and their tiers, drop how they were read.

    A frontend has to tell an entry fee from an exit fee, show a range
    and show a holding-period tier, so every quantitative field of an
    item and of a tier survives. What does not survive is the source
    line the rate was read from.
    """

    if not isinstance(value, dict):
        return None

    items: list[dict[str, Any]] = []

    for raw_item in value.get("items") or []:
        cleaned = _picked(raw_item, _FEE_ITEM_KEYS)

        if cleaned is None:
            continue

        cleaned["tiers"] = _picked_list(
            raw_item,
            "tiers",
            _FEE_TIER_KEYS,
        )

        items.append(cleaned)

    return {"items": items} if items else None


def _clean_assets(value: Any) -> Any:
    return _picked(value, _AUM_KEYS)


def _clean_party(value: Any) -> Any:
    return _picked(value, _PARTY_KEYS)


def _clean_aum_history(value: Any) -> Any:
    observations = _picked_list(
        value,
        "observations",
        _CAPITAL_OBSERVATION_KEYS,
    )

    return {"observations": observations} if observations else None


def _clean_annual_returns(value: Any) -> Any:
    observations = _picked_list(
        value,
        "observations",
        _ANNUAL_RETURN_KEYS,
    )

    return {"observations": observations} if observations else None


def _clean_historical_values(value: Any) -> Any:
    """Keep every dated series, so a chart can still be drawn from it."""

    if not isinstance(value, dict):
        return None

    series: list[dict[str, Any]] = []

    for raw_series in value.get("series") or []:
        cleaned = _picked(raw_series, _HISTORICAL_SERIES_KEYS)

        if cleaned is None:
            continue

        cleaned["observations"] = _picked_list(
            raw_series,
            "observations",
            _HISTORICAL_OBSERVATION_KEYS,
        )

        if cleaned["observations"]:
            series.append(cleaned)

    return {"series": series} if series else None


def _clean_news(value: Any) -> Any:
    items = _picked_list(value, "items", _NEWS_KEYS)

    return {"items": items} if items else None


_CLEANERS: Final[dict[str, Any]] = {
    "investment_horizon": _clean_horizon,
    "minimum_investment": _clean_minimum_investment,
    "target_return": _clean_target_return,
    "fees": _clean_fees,
    "assets_under_management": _clean_assets,
    "manager": _clean_party,
    "administrator": _clean_party,
    "aum_history": _clean_aum_history,
    "annual_returns": _clean_annual_returns,
    "historical_values": _clean_historical_values,
    "news": _clean_news,
}


def blocked_fields(
    audit_findings: Iterable[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    """
    Return the fund-and-field pairs an audit refuses to deliver.

    The audit reports only what it doubts, so a field it says nothing
    about was valid. Anything it did report — suspicious, conflicting or
    rejected — is withheld, which is what makes the export conservative.
    """

    blocked: set[tuple[str, str]] = set()

    for finding in audit_findings:
        status = str(finding.get("status") or "")

        if status not in BLOCKING_AUDIT_STATUSES:
            continue

        fund_id = str(finding.get("fund_id") or "")

        field = str(finding.get("field") or "")

        if fund_id and field:
            blocked.add((fund_id, field))

    return blocked


def delivered_field(
    *,
    field: str,
    payload: Any,
    withheld: bool,
    include_source_url: bool = True,
) -> dict[str, Any]:
    """
    Convert one internal field into its delivered form.

    Only a value the extraction settled and the audit did not doubt is
    delivered. Everything else is an absence: no value is invented, and
    no reason is given, because the reasons live in the internal output
    and in the audit report.
    """

    absent: dict[str, Any] = {
        "status": STATUS_NOT_FOUND,
        "value": None,
    }

    if withheld or not isinstance(payload, dict):
        return absent

    if str(payload.get("status") or "") != STATUS_FOUND:
        return absent

    cleaner = _CLEANERS.get(field)

    value = cleaner(payload.get("value")) if cleaner is not None else None

    if value is None:
        return absent

    delivered: dict[str, Any] = {
        "status": STATUS_FOUND,
        "value": value,
    }

    if include_source_url:
        # The address of the document is the one piece of provenance a
        # reader outside this project can act on. Everything else about
        # the source stays internal.
        url = _source_url(payload.get("source"))

        if url:
            delivered["source_url"] = url

    return delivered


def _source_url(
    source: Any,
) -> str | None:
    """Read the document address out of either stored evidence shape."""

    if not isinstance(source, dict):
        return None

    inner = source.get("source")

    holder = inner if isinstance(inner, dict) else source

    url = holder.get("url")

    return str(url) if url else None


def build_delivery_records(
    *,
    records: Sequence[Mapping[str, Any]],
    audit_findings: Iterable[Mapping[str, Any]] = (),
    include_source_url: bool = True,
) -> list[dict[str, Any]]:
    """Convert the internal output into the delivered one."""

    withheld = blocked_fields(audit_findings)

    delivered: list[dict[str, Any]] = []

    for record in records:
        fund_id = str(record.get("fund_id") or "")

        entry: dict[str, Any] = {
            "name": str(record.get("name") or ""),
            "web": record.get("web") or None,
        }

        for field in DELIVERY_FIELDS:
            entry[field] = delivered_field(
                field=field,
                payload=record.get(field),
                withheld=(fund_id, field) in withheld,
                include_source_url=include_source_url,
            )

        delivered.append(entry)

    return delivered


def load_records(
    path: Path,
) -> list[dict[str, Any]]:
    """Read the internal output without enforcing the full model."""

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeliveryExportError(f"Input file does not exist: {path}") from exc
    except OSError as exc:
        raise DeliveryExportError(f"Input file could not be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryExportError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, list):
        raise DeliveryExportError("The root JSON value must be an array of funds")

    return [item for item in payload if isinstance(item, dict)]


def load_audit_findings(
    path: Path | None,
) -> list[dict[str, Any]]:
    """Read the findings of an audit report, or nothing when absent."""

    if path is None:
        return []

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeliveryExportError(f"Audit report does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryExportError(f"Audit report could not be read: {path}: {exc}") from exc

    findings = payload.get("findings") if isinstance(payload, dict) else None

    if not isinstance(findings, list):
        raise DeliveryExportError(f"Audit report contains no findings array: {path}")

    return [item for item in findings if isinstance(item, dict)]


def write_delivery_output(
    *,
    records: Sequence[Mapping[str, Any]],
    path: Path,
) -> None:
    """Write the delivered output atomically."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        temporary_path.write_text(
            json.dumps(
                list(records),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)

        raise DeliveryExportError(f"Delivery output could not be written: {path}: {exc}") from exc


def delivery_summary(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Return how many funds carry each delivered field."""

    return {
        field: sum(
            1
            for record in records
            if isinstance(record.get(field), dict) and record[field].get("status") == STATUS_FOUND
        )
        for field in DELIVERY_FIELDS
    }
