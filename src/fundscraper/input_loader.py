from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from fundscraper.models import FundInput

FUND_LIST_ADAPTER = TypeAdapter(list[FundInput])


class InputFileError(ValueError):
    """Raised when the input fund file cannot be loaded or validated."""


def select_funds_by_web(
    funds: Sequence[FundInput],
    patterns: Sequence[str],
) -> list[FundInput]:
    """
    Return the funds whose input website contains any of `patterns`.

    Matching is case-insensitive and substring-based, so an administrator
    or platform can be named without knowing the exact host. Repeated
    patterns are ORed together.

    A fund whose `web` is unknown never matches: there is nothing to test
    against, and silently including it would widen a run that was asked
    to be narrow. With no patterns the selection is every fund, which
    keeps the caller free of special cases.

    The original input order is preserved, and the input list is never
    mutated.
    """

    if not patterns:
        return list(funds)

    needles = [pattern.casefold() for pattern in patterns if pattern]

    if not needles:
        return list(funds)

    return [
        fund
        for fund in funds
        if fund.web and any(needle in fund.web.casefold() for needle in needles)
    ]


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
