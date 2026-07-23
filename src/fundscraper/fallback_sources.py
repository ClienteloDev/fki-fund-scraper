from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
)


class FallbackField(StrEnum):
    INVESTMENT_HORIZON = "investment_horizon"
    MINIMUM_INVESTMENT = "minimum_investment"
    TARGET_RETURN = "target_return"
    FEES = "fees"
    ASSETS_UNDER_MANAGEMENT = "assets_under_management"


class FallbackSourceType(StrEnum):
    MANAGER_PORTAL = "manager_portal"
    OFFICIAL_REGISTRY = "official_registry"
    SECONDARY_DATABASE = "secondary_database"


class ApprovedFallbackSource(BaseModel):
    """One explicitly approved fallback data source."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    name: str = Field(min_length=1)

    base_url: HttpUrl

    source_type: FallbackSourceType

    enabled: bool = True

    priority: int = Field(
        default=100,
        ge=1,
        le=1000,
    )

    supported_fields: list[FallbackField] = Field(min_length=1)

    allowed_domains: list[str] = Field(default_factory=list)

    notes: str | None = None


SOURCE_LIST_ADAPTER = TypeAdapter(list[ApprovedFallbackSource])


class FallbackSourceConfigError(ValueError):
    """Raised when fallback source configuration is invalid."""


def load_approved_fallback_sources(
    path: Path,
) -> list[ApprovedFallbackSource]:
    """Load and validate the approved fallback source registry."""

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise FallbackSourceConfigError(
            f"Fallback source configuration does not exist: {path}"
        ) from exc
    except OSError as exc:
        raise FallbackSourceConfigError(
            f"Fallback source configuration could not be read: {path}: {exc}"
        ) from exc

    try:
        payload: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise FallbackSourceConfigError(
            "Fallback source configuration contains invalid JSON "
            f"at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    try:
        sources = SOURCE_LIST_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise FallbackSourceConfigError(
            f"Fallback source configuration is invalid:\n{exc}"
        ) from exc

    names: set[str] = set()

    for source in sources:
        normalized_name = source.name.casefold()

        if normalized_name in names:
            raise FallbackSourceConfigError(f"Fallback source names must be unique: {source.name}")

        names.add(normalized_name)

    return sorted(
        (source for source in sources if source.enabled),
        key=lambda source: (
            source.priority,
            source.name.casefold(),
        ),
    )
