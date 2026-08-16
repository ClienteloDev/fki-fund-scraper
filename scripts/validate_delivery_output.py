from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FIELD_NAMES = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
)

EXPECTED_RECORD_KEYS = {
    "name",
    "web",
    *FIELD_NAMES,
}


class DeliveryValidationError(ValueError):
    """Raised when a delivery JSON file does not match the required format."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeliveryValidationError(f"Delivery output does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryValidationError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def require_exact_keys(
    value: dict[str, Any],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)

    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DeliveryValidationError(f"{label} has invalid keys; missing={missing}, extra={extra}")


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryValidationError(f"{label} must be a non-empty string")
    return value.strip()


def validate_url(value: Any, label: str) -> None:
    text = require_string(value, label)
    parsed = urlparse(text)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeliveryValidationError(f"{label} must be a valid HTTP(S) URL")


def validate_date(value: Any, label: str) -> None:
    text = require_string(value, label)

    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryValidationError(f"{label} must be ISO 8601") from exc


def validate_found_field(field: dict[str, Any], label: str) -> None:
    require_exact_keys(
        field,
        {"status", "value", "source"},
        label,
    )

    if field["value"] is None:
        raise DeliveryValidationError(f"{label}.value must not be null")

    source = field["source"]

    if not isinstance(source, dict):
        raise DeliveryValidationError(f"{label}.source must be an object")

    allowed_source_keys = {
        "url",
        "retrieved_at",
        "published_at",
    }
    required_source_keys = {
        "url",
        "retrieved_at",
    }

    actual_source_keys = set(source)

    if not required_source_keys.issubset(actual_source_keys):
        raise DeliveryValidationError(f"{label}.source must contain url and retrieved_at")

    if not actual_source_keys.issubset(allowed_source_keys):
        raise DeliveryValidationError(
            f"{label}.source contains unsupported keys: "
            f"{sorted(actual_source_keys - allowed_source_keys)}"
        )

    validate_url(source["url"], f"{label}.source.url")
    validate_date(
        source["retrieved_at"],
        f"{label}.source.retrieved_at",
    )

    if "published_at" in source:
        validate_date(
            source["published_at"],
            f"{label}.source.published_at",
        )


def validate_not_found_field(
    field: dict[str, Any],
    label: str,
) -> None:
    require_exact_keys(
        field,
        {"status", "reason"},
        label,
    )

    reason = field["reason"]

    if not isinstance(reason, dict):
        raise DeliveryValidationError(f"{label}.reason must be an object")

    require_exact_keys(
        reason,
        {"code", "detail"},
        f"{label}.reason",
    )

    require_string(
        reason["code"],
        f"{label}.reason.code",
    )
    require_string(
        reason["detail"],
        f"{label}.reason.detail",
    )


def validate_record(record: Any, index: int) -> str:
    label = f"funds[{index}]"

    if not isinstance(record, dict):
        raise DeliveryValidationError(f"{label} must be an object")

    require_exact_keys(
        record,
        EXPECTED_RECORD_KEYS,
        label,
    )

    name = require_string(
        record["name"],
        f"{label}.name",
    )
    validate_url(
        record["web"],
        f"{label}.web",
    )

    for field_name in FIELD_NAMES:
        field = record[field_name]
        field_label = f"{label}.{field_name}"

        if not isinstance(field, dict):
            raise DeliveryValidationError(f"{field_label} must be an object")

        status = field.get("status")

        if status == "found":
            validate_found_field(field, field_label)
        elif status == "not_found":
            validate_not_found_field(field, field_label)
        else:
            raise DeliveryValidationError(f"{field_label}.status must be 'found' or 'not_found'")

    return name.casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the concise delivery funds JSON.")
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("data/output/funds.enriched.json"),
    )
    parser.add_argument(
        "--expected-funds",
        type=int,
        default=230,
        help="Expected number of funds. Use 0 to disable the check.",
    )
    args = parser.parse_args()

    payload = read_json(args.path.resolve())

    if not isinstance(payload, list):
        raise DeliveryValidationError("Delivery output root must be a JSON array")

    if args.expected_funds and len(payload) != args.expected_funds:
        raise DeliveryValidationError(f"Expected {args.expected_funds} funds, found {len(payload)}")

    normalized_names = [validate_record(record, index) for index, record in enumerate(payload)]

    if len(normalized_names) != len(set(normalized_names)):
        raise DeliveryValidationError("Delivery output contains duplicate fund names")

    found_fields = sum(
        1
        for record in payload
        for field_name in FIELD_NAMES
        if record[field_name]["status"] == "found"
    )

    print(f"Output file: {args.path.resolve()}")
    print(f"Funds: {len(payload)}")
    print(f"Fields found: {found_fields}/{len(payload) * len(FIELD_NAMES)}")
    print("Delivery output validation passed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
