from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fundscraper.domain_candidates import (
    CandidateSource,
    CandidateSourceName,
    default_candidate_sources,
    default_fallback_sources,
    discover_domain_candidates,
    hostname_preference,
)
from fundscraper.domain_manager_fallback import ManagerAdapterFallback
from fundscraper.domain_verification import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_MINIMUM_SCORE,
    DEFAULT_REVIEW_SCORE,
    DomainVerificationCache,
    VerificationResult,
)
from fundscraper.http_client import HttpFetcher
from fundscraper.normalization import canonical_url
from fundscraper.output_service import stable_fund_identifier

MISSING_URLS_FILE_NAME = "missing_urls.json"


class DomainBackfillError(RuntimeError):
    """Raised when the fund input file cannot be read or updated."""


class DomainResolutionStatus(StrEnum):
    """
    Outcome of resolving one fund domain.

    ``ERROR`` is reserved for hard failures, such as a fund whose every
    candidate main page failed to download. It is reported separately so
    that the run summary can distinguish infrastructure problems from
    genuinely unresolved funds.
    """

    VERIFIED = "verified"
    REVIEW_REQUIRED = "review_required"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FundEntry:
    """One raw record of the input fund file."""

    index: int
    name: str
    web: str | None

    @property
    def fund_id(self) -> str:
        return stable_fund_identifier(
            name=self.name,
            web=self.web,
        )

    @property
    def has_missing_domain(self) -> bool:
        return self.web is None or not self.web.strip()


@dataclass(frozen=True, slots=True)
class FundDomainResolution:
    """Result of resolving the official domain of one fund."""

    entry: FundEntry
    status: DomainResolutionStatus
    resolved_url: str | None
    reason: str
    candidates: tuple[VerificationResult, ...]
    warnings: tuple[str, ...]


class DomainCandidateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    domain: str
    source: str
    score: int
    accepted: bool
    reason: str
    result_url: str | None = None
    title: str | None = None
    snippet: str | None = None
    query: str | None = None
    engine: str | None = None
    rank: int | None = None
    evidence: list[str] = Field(default_factory=list)
    error: str | None = None


class UnresolvedFundReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_id: str
    name: str
    index: int
    status: DomainResolutionStatus
    reason: str
    candidates: list[DomainCandidateReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MissingDomainSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    funds_total: int
    funds_checked: int
    domains_added: int
    unresolved: int
    errors: int


class MissingDomainReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    input_path: str
    summary: MissingDomainSummary
    funds: list[UnresolvedFundReport] = Field(default_factory=list)


ProgressCallback = Callable[
    [
        int,
        int,
        FundDomainResolution,
    ],
    None,
]


def load_fund_entries(
    path: Path,
) -> list[FundEntry]:
    """
    Load raw fund records without rejecting missing website values.

    The strict input model requires a valid website, which is exactly the
    value this functionality is supposed to backfill. The raw payload is
    therefore validated only as far as this script needs.
    """

    payload = _read_payload(path)

    entries: list[FundEntry] = []

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise DomainBackfillError(f"Fund record {index} must be a JSON object")

        raw_name = item.get("name")

        if not isinstance(raw_name, str) or not raw_name.strip():
            raise DomainBackfillError(f"Fund record {index} must contain a non-empty name")

        raw_web = item.get("web")

        if raw_web is not None and not isinstance(raw_web, str):
            raise DomainBackfillError(
                f"Fund record {index} must contain a string or null web value"
            )

        entries.append(
            FundEntry(
                index=index,
                name=raw_name.strip(),
                web=raw_web,
            )
        )

    return entries


def select_funds_with_missing_domain(
    entries: list[FundEntry],
) -> list[FundEntry]:
    """Return only funds whose website value is null, empty or blank."""

    return [entry for entry in entries if entry.has_missing_domain]


async def resolve_fund_domain(
    *,
    entry: FundEntry,
    fetcher: HttpFetcher,
    sources: tuple[CandidateSource, ...],
    verification_cache: DomainVerificationCache,
    fallback_sources: tuple[CandidateSource, ...] = (),
    adapter_sources: tuple[CandidateSource, ...] = (),
    max_candidates: int = 8,
    minimum_score: int = DEFAULT_MINIMUM_SCORE,
    review_score: int = DEFAULT_REVIEW_SCORE,
    ambiguity_margin: int = DEFAULT_AMBIGUITY_MARGIN,
    force: bool = False,
) -> FundDomainResolution:
    """
    Discover and verify the official website of one fund.

    Standalone websites found by search are attempted first. Only when
    none of them verifies is the fund looked up in the catalogs of known
    managers and administrators through their adapters.
    """

    discovery = await discover_domain_candidates(
        fund_name=entry.name,
        fetcher=fetcher,
        sources=sources,
        fallback_sources=fallback_sources,
        max_candidates=max_candidates,
        force=force,
    )

    warnings = list(discovery.warnings)

    results: list[VerificationResult] = []

    for candidate in discovery.candidates:
        results.append(
            await verification_cache.verify(
                fund_name=entry.name,
                candidate=candidate,
                fetcher=fetcher,
                minimum_score=minimum_score,
                force=force,
            )
        )

    if adapter_sources and not any(result.accepted for result in results):
        adapter_discovery = await discover_domain_candidates(
            fund_name=entry.name,
            fetcher=fetcher,
            sources=adapter_sources,
            max_candidates=max_candidates,
            minimum_primary_candidates=max_candidates,
            force=force,
        )

        warnings.extend(adapter_discovery.warnings)

        verified_urls = {result.page_url for result in results}

        for candidate in adapter_discovery.candidates:
            if candidate.result_url in verified_urls:
                continue

            results.append(
                await verification_cache.verify(
                    fund_name=entry.name,
                    candidate=candidate,
                    fetcher=fetcher,
                    minimum_score=minimum_score,
                    force=force,
                )
            )

    if not results:
        return FundDomainResolution(
            entry=entry,
            status=DomainResolutionStatus.NOT_FOUND,
            resolved_url=None,
            reason=(
                "No standalone official website was found and the fund "
                "was not listed by any known manager or administrator."
            ),
            candidates=(),
            warnings=tuple(warnings),
        )

    return decide_resolution(
        entry=entry,
        results=tuple(results),
        warnings=tuple(warnings),
        review_score=review_score,
        ambiguity_margin=ambiguity_margin,
    )


def decide_resolution(
    *,
    entry: FundEntry,
    results: tuple[VerificationResult, ...],
    warnings: tuple[str, ...],
    review_score: int = DEFAULT_REVIEW_SCORE,
    ambiguity_margin: int = DEFAULT_AMBIGUITY_MARGIN,
) -> FundDomainResolution:
    """Turn verification results into one conservative decision."""

    ranked_results = tuple(
        sorted(
            results,
            key=lambda item: (
                -item.score,
                item.candidate.rank,
                item.candidate.domain,
            ),
        )
    )

    accepted = _preferred_accepted(
        accepted=[result for result in ranked_results if result.accepted],
        fund_name=entry.name,
    )

    if len(accepted) == 1:
        return _verified(
            entry=entry,
            best=accepted[0],
            results=ranked_results,
            warnings=warnings,
        )

    if len(accepted) > 1:
        alias_best = _same_brand_best(accepted)

        if alias_best is not None:
            # ambeat.cz and ambeat.eu are the same website under two top
            # level domains, not two competing candidates.
            return _verified(
                entry=entry,
                best=alias_best,
                results=ranked_results,
                warnings=warnings,
            )

        best, runner_up = accepted[0], accepted[1]

        if best.score - runner_up.score >= ambiguity_margin:
            return _verified(
                entry=entry,
                best=best,
                results=ranked_results,
                warnings=warnings,
            )

        return FundDomainResolution(
            entry=entry,
            status=(DomainResolutionStatus.REVIEW_REQUIRED),
            resolved_url=None,
            reason=(
                f"{len(accepted)} candidate domains passed verification with "
                f"similar scores ({best.candidate.domain}: {best.score}, "
                f"{runner_up.candidate.domain}: {runner_up.score}). "
                "The exact official domain is ambiguous."
            ),
            candidates=ranked_results,
            warnings=warnings,
        )

    if (
        ranked_results
        and all(result.error is not None for result in ranked_results)
        # A generated domain that does not resolve is an ordinary
        # negative result of guessing, not a technical failure. Only a
        # real search or adapter result that could not be downloaded
        # points at an infrastructure problem worth reporting.
        and any(
            result.candidate.source != CandidateSourceName.HEURISTIC for result in ranked_results
        )
    ):
        return FundDomainResolution(
            entry=entry,
            status=DomainResolutionStatus.ERROR,
            resolved_url=None,
            reason=("None of the candidate domains could be downloaded for verification."),
            candidates=ranked_results,
            warnings=warnings,
        )

    if ranked_results and ranked_results[0].score >= review_score:
        return FundDomainResolution(
            entry=entry,
            status=(DomainResolutionStatus.REVIEW_REQUIRED),
            resolved_url=None,
            reason=(
                f"The best candidate {ranked_results[0].candidate.domain} "
                f"reached score {ranked_results[0].score} without passing "
                f"verification. {ranked_results[0].reason}"
            ),
            candidates=ranked_results,
            warnings=warnings,
        )

    return FundDomainResolution(
        entry=entry,
        status=DomainResolutionStatus.NOT_FOUND,
        resolved_url=None,
        reason="No candidate domain provided a sufficiently clear match to the fund.",
        candidates=ranked_results,
        warnings=warnings,
    )


async def resolve_missing_domains(
    *,
    entries: list[FundEntry],
    fetcher: HttpFetcher,
    sources: tuple[CandidateSource, ...] | None = None,
    fallback_sources: tuple[CandidateSource, ...] | None = None,
    adapter_sources: tuple[CandidateSource, ...] | None = None,
    concurrency: int = 4,
    max_candidates: int = 8,
    minimum_score: int = DEFAULT_MINIMUM_SCORE,
    review_score: int = DEFAULT_REVIEW_SCORE,
    ambiguity_margin: int = DEFAULT_AMBIGUITY_MARGIN,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> list[FundDomainResolution]:
    """Resolve all supplied funds concurrently without stopping on failure."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least one")

    active_sources = sources if sources is not None else default_candidate_sources()

    active_fallback_sources = (
        fallback_sources if fallback_sources is not None else default_fallback_sources()
    )

    active_adapter_sources = (
        adapter_sources if adapter_sources is not None else (ManagerAdapterFallback(),)
    )

    verification_cache = DomainVerificationCache()

    semaphore = asyncio.Semaphore(concurrency)

    progress_lock = asyncio.Lock()

    total = len(entries)

    completed = 0

    resolutions: list[FundDomainResolution | None] = [None] * total

    async def resolve_one(
        position: int,
        entry: FundEntry,
    ) -> None:
        nonlocal completed

        async with semaphore:
            try:
                resolution = await resolve_fund_domain(
                    entry=entry,
                    fetcher=fetcher,
                    sources=active_sources,
                    verification_cache=(verification_cache),
                    fallback_sources=(active_fallback_sources),
                    adapter_sources=(active_adapter_sources),
                    max_candidates=max_candidates,
                    minimum_score=minimum_score,
                    review_score=review_score,
                    ambiguity_margin=(ambiguity_margin),
                    force=force,
                )
            except Exception as exc:
                # One failed fund must never stop the remaining funds.
                resolution = FundDomainResolution(
                    entry=entry,
                    status=(DomainResolutionStatus.ERROR),
                    resolved_url=None,
                    reason=(f"Domain resolution failed: {type(exc).__name__}: {exc}"),
                    candidates=(),
                    warnings=(),
                )

        resolutions[position] = resolution

        async with progress_lock:
            completed += 1

            if progress_callback is not None:
                progress_callback(
                    completed,
                    total,
                    resolution,
                )

    await asyncio.gather(*(resolve_one(position, entry) for position, entry in enumerate(entries)))

    return [resolution for resolution in resolutions if resolution is not None]


def update_fund_domains(
    *,
    path: Path,
    resolutions: list[FundDomainResolution],
    dry_run: bool = False,
) -> int:
    """
    Write verified domains back into the original fund file.

    The file is re-read so that unrelated records, key order and any
    additional fields are preserved. Only values that are still missing
    are updated, and the file is replaced atomically.
    """

    updates = {
        resolution.entry.index: resolution.resolved_url
        for resolution in resolutions
        if resolution.status is DomainResolutionStatus.VERIFIED and resolution.resolved_url
    }

    if not updates:
        return 0

    payload = _read_payload(path)

    updated = 0

    for index, resolved_url in sorted(updates.items()):
        if index >= len(payload):
            raise DomainBackfillError(
                f"Fund record {index} no longer exists in the input file: {path}"
            )

        item = payload[index]

        if not isinstance(item, dict):
            raise DomainBackfillError(f"Fund record {index} must be a JSON object")

        current_web = item.get("web")

        if current_web is not None and str(current_web).strip():
            # The value was filled in by someone else meanwhile.
            continue

        item["web"] = resolved_url

        updated += 1

    if updated and not dry_run:
        _write_json_atomic(
            path=path,
            payload=payload,
        )

    return updated


def build_missing_domain_report(
    *,
    input_path: Path,
    funds_total: int,
    resolutions: list[FundDomainResolution],
    domains_added: int,
    now: datetime | None = None,
) -> MissingDomainReport:
    """Build the report of funds whose domain remains unresolved."""

    unresolved = [
        resolution
        for resolution in resolutions
        if resolution.status is not DomainResolutionStatus.VERIFIED
    ]

    errors = sum(
        1 for resolution in resolutions if resolution.status is DomainResolutionStatus.ERROR
    )

    return MissingDomainReport(
        generated_at=(now or datetime.now(UTC)),
        input_path=str(input_path),
        summary=MissingDomainSummary(
            funds_total=funds_total,
            funds_checked=len(resolutions),
            domains_added=domains_added,
            unresolved=len(unresolved),
            errors=errors,
        ),
        funds=[_unresolved_report(resolution) for resolution in unresolved],
    )


def write_missing_domain_report(
    *,
    report: MissingDomainReport,
    path: Path,
) -> None:
    """Write the unresolved fund report as atomic JSON."""

    _write_json_atomic(
        path=path,
        payload=report.model_dump(mode="json"),
    )


def backup_input_file(
    *,
    path: Path,
    directory: Path,
) -> Path | None:
    """Copy the original input file before it is modified."""

    if not path.exists():
        return None

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination = directory / path.name

    shutil.copy2(
        path,
        destination,
    )

    return destination


def _preferred_accepted(
    *,
    accepted: list[VerificationResult],
    fund_name: str,
) -> list[VerificationResult]:
    """
    Keep only the most closely related of the accepted candidates.

    A third-party page that merely names the fund lives on a hostname
    unrelated to it. It must not compete with the website of the fund
    itself or with a fund profile returned by a manager adapter.
    """

    if len(accepted) < 2:
        return accepted

    def tier(
        result: VerificationResult,
    ) -> int:
        if result.candidate.source == CandidateSourceName.ADAPTER:
            return 1

        return hostname_preference(
            fund_name=fund_name,
            domain=result.candidate.domain,
        )

    best_tier = min(tier(result) for result in accepted)

    return [result for result in accepted if tier(result) == best_tier]


TOP_LEVEL_DOMAIN_PREFERENCE: tuple[str, ...] = (
    ".cz",
    ".eu",
    ".com",
)


def _same_brand_best(
    accepted: list[VerificationResult],
) -> VerificationResult | None:
    """
    Return one result when all accepted domains are the same brand.

    Funds frequently publish the same website under several top level
    domains. Those are aliases of one official website, so they must not
    be reported as an ambiguous choice between different candidates.
    """

    labels = {result.candidate.domain.split(".")[0] for result in accepted}

    if len(labels) != 1:
        return None

    def preference(
        result: VerificationResult,
    ) -> tuple[int, int, int]:
        domain = result.candidate.domain

        top_level_rank = next(
            (
                index
                for index, suffix in enumerate(TOP_LEVEL_DOMAIN_PREFERENCE)
                if domain.endswith(suffix)
            ),
            len(TOP_LEVEL_DOMAIN_PREFERENCE),
        )

        return (
            -result.score,
            top_level_rank,
            result.candidate.rank,
        )

    return min(
        accepted,
        key=preference,
    )


def _verified(
    *,
    entry: FundEntry,
    best: VerificationResult,
    results: tuple[VerificationResult, ...],
    warnings: tuple[str, ...],
) -> FundDomainResolution:
    return FundDomainResolution(
        entry=entry,
        status=DomainResolutionStatus.VERIFIED,
        resolved_url=_resolved_url(best),
        reason=best.reason,
        candidates=results,
        warnings=warnings,
    )


def _resolved_url(
    best: VerificationResult,
) -> str:
    """
    Return the most fund-specific URL of a verified candidate.

    A catalog section is addressed by a fragment, which the HTTP layer
    strips before downloading. The verified page URL would therefore
    point at the shared catalog page instead of the exact fund section.
    """

    result_url = best.candidate.result_url

    if (
        result_url
        and "#" in result_url
        and best.page_url
        and canonical_url(result_url) == canonical_url(best.page_url)
    ):
        return result_url

    return best.page_url or best.candidate.url


def _unresolved_report(
    resolution: FundDomainResolution,
) -> UnresolvedFundReport:
    return UnresolvedFundReport(
        fund_id=resolution.entry.fund_id,
        name=resolution.entry.name,
        index=resolution.entry.index,
        status=resolution.status,
        reason=resolution.reason,
        candidates=[
            DomainCandidateReport(
                url=(result.page_url or result.candidate.result_url or result.candidate.url),
                domain=result.candidate.domain,
                source=result.candidate.source,
                score=result.score,
                accepted=result.accepted,
                reason=result.reason,
                result_url=(result.candidate.result_url),
                title=(result.candidate.title or None),
                snippet=(result.candidate.snippet or None),
                query=(result.candidate.query or None),
                engine=(result.candidate.engine or None),
                rank=result.candidate.rank,
                evidence=list(result.evidence),
                error=result.error,
            )
            for result in resolution.candidates
        ],
        warnings=list(resolution.warnings),
    )


def _read_payload(
    path: Path,
) -> list[Any]:
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise DomainBackfillError(f"Input file does not exist: {path}") from exc
    except OSError as exc:
        raise DomainBackfillError(f"Input file could not be read: {path}: {exc}") from exc

    try:
        payload: object = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DomainBackfillError(
            f"Input file contains invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, list):
        raise DomainBackfillError("The root JSON value must be an array of funds")

    return payload


def _write_json_atomic(
    *,
    path: Path,
    payload: object,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        temporary_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)

        raise DomainBackfillError(f"File could not be written: {path}: {exc}") from exc
