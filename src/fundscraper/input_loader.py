from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from fundscraper.models import FundInput

FUND_LIST_ADAPTER = TypeAdapter(list[FundInput])


class InputFileError(ValueError):
    """Raised when the input fund file cannot be loaded or validated."""


def load_funds(path: Path) -> list[FundInput]:
    """
    Load and validate the original list of funds.

    The function preserves the original order of records.
    """

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise InputFileError(f"Input file does not exist: {path}") from exc
    except OSError as exc:
        raise InputFileError(f"Input file could not be read: {path}: {exc}") from exc

    try:
        payload: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InputFileError(
            f"Input file contains invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, list):
        raise InputFileError("The root JSON value must be an array of funds")

    try:
        funds = FUND_LIST_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise InputFileError(f"Input validation failed:\n{exc}") from exc

    if not funds:
        raise InputFileError("The input file contains no funds")

    return funds
