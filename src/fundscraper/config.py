from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Self

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class HttpSettings:
    """Configuration used by the asynchronous HTTP client."""

    timeout_seconds: float = 30.0
    max_retries: int = 3
    max_concurrency: int = 5
    requests_per_second: float = 2.0
    max_response_bytes: int = 50_000_000
    retry_min_wait_seconds: float = 0.5
    retry_max_wait_seconds: float = 5.0
    user_agent: str = "fundscraper/0.1"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ConfigurationError("HTTP timeout must be greater than zero")

        if self.max_retries < 0:
            raise ConfigurationError("HTTP max retries must not be negative")

        if self.max_concurrency < 1:
            raise ConfigurationError("HTTP concurrency must be at least one")

        if self.requests_per_second <= 0:
            raise ConfigurationError("HTTP rate limit must be greater than zero")

        if self.max_response_bytes < 1:
            raise ConfigurationError("HTTP maximum response size must be positive")

        if self.retry_min_wait_seconds < 0:
            raise ConfigurationError("HTTP minimum retry wait must not be negative")

        if self.retry_max_wait_seconds < self.retry_min_wait_seconds:
            raise ConfigurationError(
                "HTTP maximum retry wait must not be lower than the minimum retry wait"
            )

        if not self.user_agent.strip():
            raise ConfigurationError("HTTP user agent must not be empty")

    @classmethod
    def from_environment(cls) -> Self:
        """Load HTTP settings from .env and environment variables."""

        load_dotenv()

        return cls(
            timeout_seconds=_read_float(
                "HTTP_TIMEOUT_SECONDS",
                30.0,
            ),
            max_retries=_read_int(
                "HTTP_MAX_RETRIES",
                3,
            ),
            max_concurrency=_read_int(
                "HTTP_CONCURRENCY",
                5,
            ),
            requests_per_second=_read_float(
                "HTTP_REQUESTS_PER_SECOND",
                2.0,
            ),
            max_response_bytes=_read_int(
                "HTTP_MAX_RESPONSE_BYTES",
                50_000_000,
            ),
            retry_min_wait_seconds=_read_float(
                "HTTP_RETRY_MIN_WAIT_SECONDS",
                0.5,
            ),
            retry_max_wait_seconds=_read_float(
                "HTTP_RETRY_MAX_WAIT_SECONDS",
                5.0,
            ),
            user_agent=os.getenv(
                "HTTP_USER_AGENT",
                "fundscraper/0.1",
            ),
        )


def _read_int(
    name: str,
    default: int,
) -> int:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _read_float(
    name: str,
    default: float,
) -> float:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
