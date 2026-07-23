from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fundscraper.config import HttpSettings
from fundscraper.http_client import (
    HttpFetcher,
    ResponseTooLargeError,
)


def test_fetches_and_uses_cache(
    tmp_path: Path,
) -> None:
    calls = 0

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1

        return httpx.Response(
            status_code=200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
            },
            content=b"<html>Example</html>",
            request=request,
        )

    async def run_test() -> None:
        transport = httpx.MockTransport(handler)

        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=transport,
        ) as fetcher:
            first_result = await fetcher.fetch("https://example.com")

            second_result = await fetcher.fetch("https://example.com")

        assert first_result.from_cache is False
        assert second_result.from_cache is True
        assert first_result.body == b"<html>Example</html>"
        assert first_result.content_type == "text/html"
        assert first_result.sha256 == second_result.sha256

    asyncio.run(run_test())

    assert calls == 1


def test_retries_retryable_status(
    tmp_path: Path,
) -> None:
    calls = 0

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1

        if calls == 1:
            return httpx.Response(
                status_code=503,
                request=request,
            )

        return httpx.Response(
            status_code=200,
            content=b"success",
            request=request,
        )

    async def run_test() -> None:
        transport = httpx.MockTransport(handler)

        settings = HttpSettings(
            max_retries=1,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=transport,
        ) as fetcher:
            result = await fetcher.fetch("https://example.com")

        assert result.status_code == 200
        assert result.body == b"success"

    asyncio.run(run_test())

    assert calls == 2


def test_rejects_large_response(
    tmp_path: Path,
) -> None:
    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={
                "Content-Length": "100",
            },
            content=b"x" * 100,
            request=request,
        )

    async def run_test() -> None:
        transport = httpx.MockTransport(handler)

        settings = HttpSettings(
            max_retries=0,
            max_response_bytes=10,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=transport,
        ) as fetcher:
            with pytest.raises(
                ResponseTooLargeError,
                match="maximum allowed size",
            ):
                await fetcher.fetch("https://example.com")

    asyncio.run(run_test())


def test_force_bypasses_cache(
    tmp_path: Path,
) -> None:
    calls = 0

    async def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal calls
        calls += 1

        return httpx.Response(
            status_code=200,
            content=f"response-{calls}".encode(),
            request=request,
        )

    async def run_test() -> None:
        transport = httpx.MockTransport(handler)

        settings = HttpSettings(
            max_retries=0,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
        )

        async with HttpFetcher(
            settings,
            tmp_path / "http",
            transport=transport,
        ) as fetcher:
            first_result = await fetcher.fetch("https://example.com")

            second_result = await fetcher.fetch(
                "https://example.com",
                force=True,
            )

        assert first_result.body == b"response-1"
        assert second_result.body == b"response-2"

    asyncio.run(run_test())

    assert calls == 2
