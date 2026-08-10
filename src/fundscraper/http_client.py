from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from fundscraper.config import HttpSettings

RETRYABLE_STATUS_CODES = frozenset(
    {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }
)


class FetchError(RuntimeError):
    """Base error raised by the HTTP fetching layer."""

    code = "fetch_error"

    def __init__(
        self,
        message: str,
        *,
        url: str,
    ) -> None:
        super().__init__(message)
        self.url = url


class InvalidFetchUrlError(FetchError):
    code = "invalid_url"


class NetworkFetchError(FetchError):
    code = "network_error"


class HttpStatusFetchError(FetchError):
    code = "http_status_error"

    def __init__(
        self,
        message: str,
        *,
        url: str,
        status_code: int,
    ) -> None:
        super().__init__(
            message,
            url=url,
        )
        self.status_code = status_code


class RetryableHttpStatusError(HttpStatusFetchError):
    code = "retryable_http_status"


class ResponseTooLargeError(FetchError):
    code = "response_too_large"


class HttpCacheError(FetchError):
    code = "cache_error"


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str | None
    retrieved_at: datetime
    sha256: str
    body: bytes
    local_path: Path
    from_cache: bool


@dataclass(frozen=True, slots=True)
class CacheMetadata:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str | None
    retrieved_at: datetime
    sha256: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "retrieved_at": self.retrieved_at.isoformat(),
            "sha256": self.sha256,
        }

    @classmethod
    def from_json_dict(
        cls,
        payload: dict[str, Any],
    ) -> Self:
        try:
            retrieved_at = datetime.fromisoformat(str(payload["retrieved_at"]))

            if retrieved_at.tzinfo is None:
                raise ValueError("retrieved_at does not contain timezone information")

            return cls(
                requested_url=str(payload["requested_url"]),
                final_url=str(payload["final_url"]),
                status_code=int(payload["status_code"]),
                content_type=(
                    str(payload["content_type"])
                    if payload.get("content_type") is not None
                    else None
                ),
                retrieved_at=retrieved_at,
                sha256=str(payload["sha256"]),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError("Invalid HTTP cache metadata") from exc


class HttpCache:
    """Filesystem cache for downloaded HTTP response bodies."""

    def __init__(
        self,
        directory: Path,
    ) -> None:
        self.directory = directory

    def load(
        self,
        url: str,
    ) -> FetchResult | None:
        body_path, metadata_path = self._paths(url)

        if not body_path.exists() and not metadata_path.exists():
            return None

        if not body_path.exists() or not metadata_path.exists():
            raise HttpCacheError(
                "HTTP cache entry is incomplete",
                url=url,
            )

        try:
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))

            if not isinstance(metadata_payload, dict):
                raise ValueError("Cache metadata root must be an object")

            metadata = CacheMetadata.from_json_dict(metadata_payload)

            body = body_path.read_bytes()
        except (
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise HttpCacheError(
                f"Could not load HTTP cache entry: {exc}",
                url=url,
            ) from exc

        body_hash = hashlib.sha256(body).hexdigest()

        if body_hash != metadata.sha256:
            raise HttpCacheError(
                "HTTP cache body hash does not match metadata",
                url=url,
            )

        return FetchResult(
            requested_url=metadata.requested_url,
            final_url=metadata.final_url,
            status_code=metadata.status_code,
            content_type=metadata.content_type,
            retrieved_at=metadata.retrieved_at,
            sha256=metadata.sha256,
            body=body,
            local_path=body_path,
            from_cache=True,
        )

    def save(
        self,
        result: FetchResult,
    ) -> FetchResult:
        self.directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        body_path, metadata_path = self._paths(result.requested_url)

        metadata = CacheMetadata(
            requested_url=result.requested_url,
            final_url=result.final_url,
            status_code=result.status_code,
            content_type=result.content_type,
            retrieved_at=result.retrieved_at,
            sha256=result.sha256,
        )

        temporary_body_path = body_path.with_suffix(f"{body_path.suffix}.tmp")

        temporary_metadata_path = metadata_path.with_suffix(f"{metadata_path.suffix}.tmp")

        try:
            temporary_body_path.write_bytes(result.body)

            temporary_metadata_path.write_text(
                json.dumps(
                    metadata.to_json_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            temporary_body_path.replace(body_path)

            temporary_metadata_path.replace(metadata_path)
        except OSError as exc:
            temporary_body_path.unlink(missing_ok=True)

            temporary_metadata_path.unlink(missing_ok=True)

            raise HttpCacheError(
                f"Could not save HTTP cache entry: {exc}",
                url=result.requested_url,
            ) from exc

        return FetchResult(
            requested_url=result.requested_url,
            final_url=result.final_url,
            status_code=result.status_code,
            content_type=result.content_type,
            retrieved_at=result.retrieved_at,
            sha256=result.sha256,
            body=result.body,
            local_path=body_path,
            from_cache=False,
        )

    def _paths(
        self,
        url: str,
    ) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()

        return (
            self.directory / f"{key}.body",
            self.directory / f"{key}.json",
        )


class HttpFetcher:
    """Asynchronous HTTP client with retries, limits and cache."""

    def __init__(
        self,
        settings: HttpSettings,
        cache_directory: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cache = HttpCache(cache_directory)

        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

        self._limiter = AsyncLimiter(
            settings.requests_per_second,
            time_period=1.0,
        )

        self._url_locks: dict[
            str,
            asyncio.Lock,
        ] = {}

        self._domain_semaphores: dict[
            str,
            asyncio.Semaphore,
        ] = {}

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds),
            limits=httpx.Limits(
                max_connections=settings.max_concurrency,
                max_keepalive_connections=settings.max_concurrency,
            ),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/pdf,application/json;q=0.9,"
                    "*/*;q=0.8"
                ),
            },
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        force: bool = False,
    ) -> FetchResult:
        normalized_url = normalize_request_url(url)

        lock = self._url_locks.setdefault(
            normalized_url,
            asyncio.Lock(),
        )

        async with lock:
            if not force:
                cached_result = self.cache.load(normalized_url)

                if cached_result is not None:
                    return cached_result

            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self.settings.max_retries + 1),
                    wait=wait_exponential_jitter(
                        initial=(self.settings.retry_min_wait_seconds),
                        max=(self.settings.retry_max_wait_seconds),
                        jitter=(self.settings.retry_min_wait_seconds),
                    ),
                    retry=retry_if_exception_type(
                        (
                            httpx.TransportError,
                            RetryableHttpStatusError,
                        )
                    ),
                    reraise=True,
                ):
                    with attempt:
                        result = await self._request_once(normalized_url)

                        return self.cache.save(result)
            except httpx.TransportError as exc:
                raise NetworkFetchError(
                    f"HTTP network request failed: {exc}",
                    url=normalized_url,
                ) from exc
            except httpx.HTTPError as exc:
                # A redirect loop or a malformed response is a failure of
                # one address, not of the run. Letting it escape as an
                # httpx error killed a whole crawl at the fifty-sixth
                # fund, so every HTTP failure leaves here as a FetchError.
                raise NetworkFetchError(
                    f"HTTP request failed: {type(exc).__name__}: {exc}",
                    url=normalized_url,
                ) from exc

        raise NetworkFetchError(
            "HTTP request did not produce a result",
            url=normalized_url,
        )

    async def _request_once(
        self,
        url: str,
    ) -> FetchResult:
        parsed_url = httpx.URL(url)
        domain_key = parsed_url.host or ""

        domain_semaphore = self._domain_semaphores.setdefault(
            domain_key,
            asyncio.Semaphore(
                self.settings.max_per_domain_concurrency,
            ),
        )

        async with (
            self._semaphore,
            domain_semaphore,
            self._limiter,
            self._client.stream(
                "GET",
                url,
            ) as response,
        ):
            if response.status_code in RETRYABLE_STATUS_CODES:
                raise RetryableHttpStatusError(
                    f"Server returned retryable HTTP status {response.status_code}",
                    url=url,
                    status_code=response.status_code,
                )

            if response.is_error:
                raise HttpStatusFetchError(
                    f"Server returned HTTP status {response.status_code}",
                    url=url,
                    status_code=response.status_code,
                )

            self._validate_content_length(
                response,
                url,
            )

            body_buffer = bytearray()

            async for chunk in response.aiter_bytes():
                body_buffer.extend(chunk)

                if len(body_buffer) > self.settings.max_response_bytes:
                    raise ResponseTooLargeError(
                        "Response exceeded maximum "
                        "allowed size of "
                        f"{self.settings.max_response_bytes} bytes",
                        url=url,
                    )

            body = bytes(body_buffer)

            content_type = _content_type(response.headers.get("content-type"))

            retrieved_at = datetime.now(UTC)

            return FetchResult(
                requested_url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=content_type,
                retrieved_at=retrieved_at,
                sha256=hashlib.sha256(body).hexdigest(),
                body=body,
                local_path=Path(),
                from_cache=False,
            )

    def _validate_content_length(
        self,
        response: httpx.Response,
        url: str,
    ) -> None:
        raw_content_length = response.headers.get("content-length")

        if raw_content_length is None:
            return

        try:
            content_length = int(raw_content_length)
        except ValueError:
            return

        if content_length > self.settings.max_response_bytes:
            raise ResponseTooLargeError(
                "Response Content-Length exceeds "
                "maximum allowed size of "
                f"{self.settings.max_response_bytes} bytes",
                url=url,
            )


def normalize_request_url(
    url: str,
) -> str:
    """Validate an HTTP URL and remove its fragment."""

    try:
        parsed_url = httpx.URL(url.strip())
    except httpx.InvalidURL as exc:
        raise InvalidFetchUrlError(
            f"Invalid URL: {url}",
            url=url,
        ) from exc

    if parsed_url.scheme not in {
        "http",
        "https",
    }:
        raise InvalidFetchUrlError(
            "URL must use HTTP or HTTPS",
            url=url,
        )

    if parsed_url.host is None:
        raise InvalidFetchUrlError(
            "URL must contain a hostname",
            url=url,
        )

    return str(parsed_url.copy_with(fragment=None))


def _content_type(
    raw_content_type: str | None,
) -> str | None:
    if raw_content_type is None:
        return None

    normalized = (
        raw_content_type.split(
            ";",
            maxsplit=1,
        )[0]
        .strip()
        .lower()
    )

    return normalized or None
