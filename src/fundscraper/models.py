from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FundInput(BaseModel):
    """One fund record loaded from the original funds.json file."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    name: str = Field(min_length=1)
    web: str = Field(min_length=1)

    @field_validator("web")
    @classmethod
    def validate_web_url(cls, value: str) -> str:
        parsed = urlparse(value)

        if parsed.scheme not in {"http", "https"}:
            raise ValueError("web must use the http or https protocol")

        if not parsed.netloc:
            raise ValueError("web must contain a valid hostname")

        return value
