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

    # The canonical dataset carries funds whose official website is not
    # known. Such a fund is a real fund that still needs its manager
    # looked up, so it must load rather than reject the whole file.
    web: str | None = None

    @field_validator("web")
    @classmethod
    def validate_web_url(cls, value: str | None) -> str | None:
        if value is None:
            return None

        if not value.strip():
            return None

        parsed = urlparse(value)

        if parsed.scheme not in {"http", "https"}:
            raise ValueError("web must use the http or https protocol")

        if not parsed.netloc:
            raise ValueError("web must contain a valid hostname")

        return value

    @property
    def has_website(self) -> bool:
        """Return whether an official website is known for this fund."""

        return bool(self.web)
