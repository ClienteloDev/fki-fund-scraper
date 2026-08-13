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

ALLOWED_INTERNAL_STATUSES = {
    "pending",
    "found",
    "not_found",
    "ambiguous",
    "conflicting",
    "error",
}

DEFAULT_REASON_DETAILS = {
    "pending": "The field was not processed successfully before the export was created.",
    "not_found": "No sufficiently reliable public value was found in the processed sources.",
    "ambiguous": (
        "Multiple possible values or scopes were found and the correct "
        "value could not be determined reliably."
    ),
    "conflicting": "Conflicting values were found in the available sources.",
    "error": "The field could not be produced because processing ended with an error.",
}


class DeliveryExportError(ValueError):
    """Raised when the internal result cannot be converted safely."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeliveryExportError(f"Input file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryExportError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def require_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryExportError(f"{label} must be a non-empty string")
    return value.strip()


def validate_http_url(value: Any, label: str) -> str:
    url = require_non_empty_string(value, label)
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeliveryExportError(f"{label} must be an HTTP(S) URL: {url}")

    return url


def normalize_datetime(value: Any, label: str) -> str:
    text = require_non_empty_string(value, label)

    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryExportError(f"{label} must be an ISO 8601 date or datetime: {text}") from exc

    return text


def extract_source(field: dict[str, Any], label: str) -> dict[str, str]:
    evidence = field.get("source")

    if not isinstance(evidence, dict):
        raise DeliveryExportError(f"{label}.source must be an object")

    source = evidence.get("source")

    if not isinstance(source, dict):
        raise DeliveryExportError(f"{label}.source.source must be an object")

    result = {
        "url": validate_http_url(
            source.get("url"),
            f"{label}.source.source.url",
        ),
        "retrieved_at": normalize_datetime(
            source.get("retrieved_at"),
            f"{label}.source.source.retrieved_at",
        ),
    }

    published_at = source.get("published_at")
    if published_at is not None:
        result["published_at"] = normalize_datetime(
            published_at,
            f"{label}.source.source.published_at",
        )

    return result


def extract_missing_reason(
    field: dict[str, Any],
    internal_status: str,
    label: str,
) -> dict[str, str]:
    reason = field.get("reason")

    if isinstance(reason, dict):
        code = reason.get("code")
        detail = reason.get("detail")

        reason_code = code.strip() if isinstance(code, str) and code.strip() else internal_status

        if isinstance(detail, str) and detail.strip():
            reason_detail = detail.strip()
        else:
            reason_detail = DEFAULT_REASON_DETAILS[internal_status]
    else:
        reason_code = internal_status
        reason_detail = DEFAULT_REASON_DETAILS[internal_status]

    if internal_status != "not_found":
        reason_detail = f"Internal status was '{internal_status}'. {reason_detail}"

    return {
        "code": reason_code,
        "detail": reason_detail,
    }


def convert_field(
    field: Any,
    *,
    fund_name: str,
    field_name: str,
) -> dict[str, Any]:
    label = f"{fund_name}.{field_name}"

    if not isinstance(field, dict):
        return {
            "status": "not_found",
            "reason": {
                "code": "invalid_internal_field",
                "detail": (f"The internal output did not contain a valid object for {field_name}."),
            },
        }

    internal_status = str(field.get("status", "")).strip()

    if internal_status not in ALLOWED_INTERNAL_STATUSES:
        return {
            "status": "not_found",
            "reason": {
                "code": "invalid_internal_status",
                "detail": (
                    f"The internal output contained an unsupported status: {internal_status!r}."
                ),
            },
        }

    if internal_status == "found":
        value = field.get("value")

        if value is None:
            raise DeliveryExportError(f"{label} is found but does not contain value")

        return {
            "status": "found",
            "value": value,
            "source": extract_source(field, label),
        }

    return {
        "status": "not_found",
        "reason": extract_missing_reason(
            field,
            internal_status,
            label,
        ),
    }


def load_internal_records(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)

    if isinstance(payload, dict):
        payload = payload.get("funds", payload.get("results"))

    if not isinstance(payload, list):
        raise DeliveryExportError(
            "Internal output must be a JSON array or an object containing a 'funds' array"
        )

    records: list[dict[str, Any]] = []

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise DeliveryExportError(f"Internal record at index {index} must be an object")
        records.append(item)

    return records


def convert_record(record: dict[str, Any], index: int) -> dict[str, Any]:
    name = require_non_empty_string(
        record.get("name"),
        f"records[{index}].name",
    )
    web = validate_http_url(
        record.get("web"),
        f"records[{index}].web",
    )

    result: dict[str, Any] = {
        "name": name,
        "web": web,
    }

    for field_name in FIELD_NAMES:
        result[field_name] = convert_field(
            record.get(field_name),
            fund_name=name,
            field_name=field_name,
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert the detailed internal fundscraper output into "
            "the concise delivery JSON format."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/output/funds.enriched.internal.json"),
        help="Detailed internal fundscraper output.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/output/funds.enriched.json"),
        help="Concise delivery output.",
    )
    parser.add_argument(
        "--expected-funds",
        type=int,
        default=230,
        help="Expected number of funds. Use 0 to disable the check.",
    )
    args = parser.parse_args()

    records = load_internal_records(args.input.resolve())

    if args.expected_funds and len(records) != args.expected_funds:
        raise DeliveryExportError(f"Expected {args.expected_funds} funds, found {len(records)}")

    converted = [convert_record(record, index) for index, record in enumerate(records)]

    names = [record["name"].casefold() for record in converted]

    if len(names) != len(set(names)):
        raise DeliveryExportError("Delivery output contains duplicate fund names")

    write_json_atomic(args.output.resolve(), converted)

    found_fields = sum(
        1
        for record in converted
        for field_name in FIELD_NAMES
        if record[field_name]["status"] == "found"
    )

    print(f"Funds exported: {len(converted)}")
    print(f"Fields found: {found_fields}/{len(converted) * len(FIELD_NAMES)}")
    print(f"Delivery output: {args.output.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
