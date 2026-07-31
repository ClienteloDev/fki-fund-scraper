from __future__ import annotations

import pytest

from fundscraper.config import (
    ConfigurationError,
    HttpSettings,
)


def test_default_http_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_variables = (
        "HTTP_TIMEOUT_SECONDS",
        "HTTP_MAX_RETRIES",
        "HTTP_CONCURRENCY",
        "HTTP_PER_DOMAIN_CONCURRENCY",
        "HTTP_REQUESTS_PER_SECOND",
        "HTTP_MAX_RESPONSE_BYTES",
        "HTTP_RETRY_MIN_WAIT_SECONDS",
        "HTTP_RETRY_MAX_WAIT_SECONDS",
        "HTTP_USER_AGENT",
    )

    for variable_name in environment_variables:
        monkeypatch.delenv(
            variable_name,
            raising=False,
        )

    settings = HttpSettings.from_environment()

    assert settings.timeout_seconds == 30
    assert settings.max_retries == 3
    assert settings.max_concurrency == 8
    assert settings.max_per_domain_concurrency == 2
    assert settings.requests_per_second == 4


def test_http_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HTTP_TIMEOUT_SECONDS",
        "15",
    )

    monkeypatch.setenv(
        "HTTP_MAX_RETRIES",
        "4",
    )

    monkeypatch.setenv(
        "HTTP_CONCURRENCY",
        "8",
    )

    monkeypatch.setenv(
        "HTTP_PER_DOMAIN_CONCURRENCY",
        "3",
    )

    settings = HttpSettings.from_environment()

    assert settings.timeout_seconds == 15
    assert settings.max_retries == 4
    assert settings.max_concurrency == 8
    assert settings.max_per_domain_concurrency == 3


def test_http_settings_reject_invalid_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HTTP_CONCURRENCY",
        "invalid",
    )

    with pytest.raises(
        ConfigurationError,
        match="must be an integer",
    ):
        HttpSettings.from_environment()


def test_http_settings_reject_invalid_range() -> None:
    with pytest.raises(
        ConfigurationError,
        match="at least one",
    ):
        HttpSettings(max_concurrency=0)
