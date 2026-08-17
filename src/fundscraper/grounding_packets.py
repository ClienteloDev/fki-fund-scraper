from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
)

from fundscraper.database import (
    ParsedDocumentRecord,
    list_parsed_documents,
)
from fundscraper.document_parser import (
    DocumentParseError,
    ParsedDocument,
    load_parsed_document,
)
from fundscraper.fallback_sources import (
    FallbackField,
)
from fundscraper.html_discovery import (
    normalize_search_text,
)
from fundscraper.models import FundInput
from fundscraper.output_models import (
    DocumentType,
    FieldStatus,
    FundOutput,
)
from fundscraper.output_service import (
    stable_fund_id,
)


class GroundingPacketError(RuntimeError):
    """Raised when grounding packets cannot be created or stored."""


@dataclass(frozen=True, slots=True)
class LoadedGroundingDocument:
    record: ParsedDocumentRecord
    document: ParsedDocument


class GroundingSnippet(BaseModel):
    """One evidence snippet selected from a parsed source."""

    model_config = ConfigDict(extra="forbid")

    snippet_id: str
    source_id: int
    url: HttpUrl
    title: str | None
    document_type: DocumentType
    retrieved_at: datetime
    page: int | None
    quote: str = Field(min_length=1)
    score: int = Field(ge=0)
    matched_keywords: list[str]
    fund_identity_matches: int = Field(ge=0)
    scope_warning: bool


class FieldGroundingPacket(BaseModel):
    """Grounded context for one unresolved fund field."""

    model_config = ConfigDict(extra="forbid")

    packet_id: str = Field(min_length=1)

    fund_id: str
    fund_name: str
    web: HttpUrl
    field: FallbackField
    current_status: FieldStatus
    current_reason: str | None
    generated_at: datetime
    snippets: list[GroundingSnippet]
    insufficient_context: bool
    instructions: list[str]
    warnings: list[str]


class GroundingBatchReport(BaseModel):
    """Grounding packets created for a batch of funds."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    funds_considered: int
    funds_with_packets: int
    packets_total: int
    packets_with_context: int
    packets_without_context: int
    packets: list[FieldGroundingPacket]


ELIGIBLE_STATUSES: Final = frozenset(
    {
        FieldStatus.NOT_FOUND,
        FieldStatus.AMBIGUOUS,
        FieldStatus.CONFLICTING,
        FieldStatus.ERROR,
    }
)


FIELD_KEYWORDS: Final[
    dict[
        FallbackField,
        tuple[str, ...],
    ]
] = {
    FallbackField.INVESTMENT_HORIZON: (
        "investicni horizont",
        "doporuceny investicni horizont",
        "doporucena doba drzeni",
        "doporucena doba investice",
        "minimalni doba investice",
        "recommended holding period",
        "investment horizon",
        "holding period",
    ),
    FallbackField.MINIMUM_INVESTMENT: (
        "minimalni investice",
        "minimalni vklad",
        "minimalni upis",
        "pocatecni investice",
        "nejnizsi investice",
        "minimum investment",
        "minimum subscription",
        "initial investment",
    ),
    FallbackField.TARGET_RETURN: (
        "cilovy vynos",
        "cilove zhodnoceni",
        "ocekavany vynos",
        "ocekavane zhodnoceni",
        "predpokladany vynos",
        "target return",
        "expected return",
        "anticipated return",
    ),
    FallbackField.FEES: (
        "vstupni poplatek",
        "vystupni poplatek",
        "vykonnostni poplatek",
        "vykonnostni odmena",
        "poplatek za obhospodarovani",
        "odmena za obhospodarovani",
        "prubezne naklady",
        "management fee",
        "performance fee",
        "entry fee",
        "exit fee",
        "ongoing costs",
        "ongoing charges",
    ),
    FallbackField.ASSETS_UNDER_MANAGEMENT: (
        "majetek fondu",
        "hodnota majetku",
        "cista aktiva",
        "cista hodnota aktiv",
        "fondovy kapital",
        "aktiva fondu",
        "assets under management",
        "fund assets",
        "net assets",
        "net asset value",
    ),
}


DOCUMENT_PRIORITY: Final[
    dict[
        FallbackField,
        dict[
            DocumentType,
            int,
        ],
    ]
] = {
    FallbackField.INVESTMENT_HORIZON: {
        DocumentType.PRIIPS_KID: 100,
        DocumentType.SUBFUND_STATUTE: 90,
        DocumentType.STATUTE: 85,
        DocumentType.MEMORANDUM: 80,
        DocumentType.FACTSHEET: 65,
        DocumentType.MARKETING_PAGE: 30,
    },
    FallbackField.MINIMUM_INVESTMENT: {
        DocumentType.SUBFUND_STATUTE: 100,
        DocumentType.STATUTE: 95,
        DocumentType.MEMORANDUM: 90,
        DocumentType.PRIIPS_KID: 75,
        DocumentType.FACTSHEET: 65,
        DocumentType.MARKETING_PAGE: 35,
    },
    FallbackField.TARGET_RETURN: {
        DocumentType.MEMORANDUM: 100,
        DocumentType.FACTSHEET: 90,
        DocumentType.INFOLETTER: 75,
        DocumentType.SUBFUND_STATUTE: 60,
        DocumentType.STATUTE: 55,
        DocumentType.MARKETING_PAGE: 40,
    },
    FallbackField.FEES: {
        DocumentType.PRIIPS_KID: 100,
        DocumentType.SUBFUND_STATUTE: 95,
        DocumentType.STATUTE: 90,
        DocumentType.MEMORANDUM: 85,
        DocumentType.FACTSHEET: 70,
        DocumentType.MARKETING_PAGE: 35,
    },
    FallbackField.ASSETS_UNDER_MANAGEMENT: {
        DocumentType.ANNUAL_REPORT: 100,
        DocumentType.FINANCIAL_STATEMENTS: 100,
        DocumentType.HALF_YEAR_REPORT: 90,
        DocumentType.FACTSHEET: 80,
        DocumentType.INFOLETTER: 70,
        DocumentType.MARKETING_PAGE: 30,
    },
}


FUND_NAME_NOISE_TOKENS: Final = frozenset(
    {
        "a",
        "as",
        "s",
        "sicav",
        "fond",
        "fund",
        "fonds",
        "investicni",
        "investment",
        "spolecnost",
        "podfond",
        "subfund",
        "otevreny",
        "uzavreny",
        "promennym",
        # "s proměnným základním kapitálem" is the legal form of a SICAV,
        # written out in the registered name of 29 of the canonical funds.
        # None of its three words tells one fund from another, so a snippet
        # from a document about any other SICAV would otherwise earn identity
        # credit for this fund.
        "zakladnim",
        "kapitalem",
    }
)


SCOPE_WARNING_PHRASES: Final = (
    "skupina spravuje",
    "investicni spolecnost spravuje",
    "spravce spravuje",
    "obhospodarovatel spravuje",
    "aktiva ve sprave skupiny",
    "majetek ve sprave skupiny",
    "celkovy objem aktiv ve sprave",
    "vsechny fondy",
    "manager manages",
    "company manages",
    "group manages",
    "total assets under management",
)


DATE_PATTERN = re.compile(
    r"""
    (?:
        \d{1,2}
        \s*[./-]\s*
        \d{1,2}
        \s*[./-]\s*
        20\d{2}
    )
    |
    (?:
        20\d{2}
        -
        \d{2}
        -
        \d{2}
    )
    """,
    re.VERBOSE,
)


PERCENT_PATTERN = re.compile(r"\d+(?:[,.]\d+)?\s*%")


MONEY_PATTERN = re.compile(
    r"""
    \d
    .{0,20}
    (?:
        czk
        |
        kc
        |
        eur
        |
        usd
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def build_grounding_packets(
    *,
    funds: list[FundInput],
    outputs: list[FundOutput],
    database_path: Path,
    max_snippets: int = 8,
    now: datetime | None = None,
) -> GroundingBatchReport:
    """Create grounded context packets for unresolved fields."""

    if max_snippets < 1:
        raise ValueError("Maximum number of grounding snippets must be positive")

    generated_at = now or datetime.now(UTC)

    outputs_by_id = {output.fund_id: output for output in outputs}

    packets: list[FieldGroundingPacket] = []

    funds_with_packets: set[str] = set()

    for fund in funds:
        fund_id = stable_fund_id(fund)

        output = outputs_by_id.get(fund_id)

        if output is None:
            continue

        unresolved_fields = [
            field
            for field in FallbackField
            if _field_status(
                output,
                field,
            )
            in ELIGIBLE_STATUSES
        ]

        if not unresolved_fields:
            continue

        records = list_parsed_documents(
            database_path,
            fund_id=fund_id,
        )

        loaded_documents, load_warnings = _load_grounding_documents(records)

        for field in unresolved_fields:
            status = _field_status(
                output,
                field,
            )

            snippets = _collect_field_snippets(
                fund_name=fund.name,
                field=field,
                documents=loaded_documents,
                max_snippets=max_snippets,
            )

            packets.append(
                FieldGroundingPacket(
                    packet_id=(f"{fund_id}:{field.value}"),
                    fund_id=fund_id,
                    fund_name=fund.name,
                    web=HttpUrl(str(fund.web)),
                    field=field,
                    current_status=status,
                    current_reason=_field_reason(
                        output,
                        field,
                    ),
                    generated_at=generated_at,
                    snippets=snippets,
                    insufficient_context=(len(snippets) == 0),
                    instructions=_field_instructions(field),
                    warnings=list(load_warnings),
                )
            )

            funds_with_packets.add(fund_id)

    packets_with_context = sum(1 for packet in packets if not packet.insufficient_context)

    return GroundingBatchReport(
        generated_at=generated_at,
        funds_considered=len(funds),
        funds_with_packets=len(funds_with_packets),
        packets_total=len(packets),
        packets_with_context=packets_with_context,
        packets_without_context=(len(packets) - packets_with_context),
        packets=packets,
    )


def write_grounding_packets(
    *,
    report: GroundingBatchReport,
    path: Path,
) -> None:
    """Write grounding packets as an atomic JSON file."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        temporary_path.write_text(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        temporary_path.replace(path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)

        raise GroundingPacketError(
            f"Grounding packets could not be written: {path}: {exc}"
        ) from exc


def load_grounding_packets(
    path: Path,
) -> GroundingBatchReport:
    """Load and validate a grounding packet report."""

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise GroundingPacketError(f"Grounding packet report does not exist: {path}") from exc
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise GroundingPacketError(
            f"Grounding packet report could not be loaded: {path}: {exc}"
        ) from exc

    try:
        return GroundingBatchReport.model_validate(payload)
    except ValueError as exc:
        raise GroundingPacketError(f"Grounding packet report is invalid: {path}: {exc}") from exc


def _load_grounding_documents(
    records: list[ParsedDocumentRecord],
) -> tuple[
    list[LoadedGroundingDocument],
    tuple[str, ...],
]:
    documents: list[LoadedGroundingDocument] = []

    warnings: list[str] = []

    for record in records:
        try:
            document = load_parsed_document(Path(record.text_path))
        except DocumentParseError as exc:
            warnings.append(f"Source {record.source_id} could not be loaded: {exc}")

            continue

        documents.append(
            LoadedGroundingDocument(
                record=record,
                document=document,
            )
        )

    return (
        documents,
        tuple(warnings),
    )


def _collect_field_snippets(
    *,
    fund_name: str,
    field: FallbackField,
    documents: list[LoadedGroundingDocument],
    max_snippets: int,
) -> list[GroundingSnippet]:
    candidates: list[GroundingSnippet] = []

    seen: set[
        tuple[
            str,
            int | None,
            str,
        ]
    ] = set()

    fund_tokens = _fund_identity_tokens(fund_name)

    keywords = FIELD_KEYWORDS[field]

    for loaded_document in documents:
        record = loaded_document.record

        document_type = _document_type(record.document_type)

        identity_text = normalize_search_text(
            " ".join(
                (
                    record.title or "",
                    record.url,
                    loaded_document.document.full_text[:3000],
                )
            )
        )

        identity_matches = sum(1 for token in fund_tokens if token in identity_text)

        for page in loaded_document.document.pages:
            lines = [line.strip() for line in page.text.splitlines() if line.strip()]

            for index, line in enumerate(lines):
                normalized_line = normalize_search_text(line)

                line_keywords = [keyword for keyword in keywords if keyword in normalized_line]

                if not line_keywords:
                    continue

                start = max(
                    0,
                    index - 2,
                )

                end = min(
                    len(lines),
                    index + 3,
                )

                quote = "\n".join(lines[start:end])

                normalized_quote = normalize_search_text(quote)

                matched_keywords = [keyword for keyword in keywords if keyword in normalized_quote]

                key = (
                    record.url,
                    page.page_number,
                    normalized_quote,
                )

                if key in seen:
                    continue

                seen.add(key)

                scope_warning = any(phrase in normalized_quote for phrase in SCOPE_WARNING_PHRASES)

                score = _snippet_score(
                    field=field,
                    document_type=document_type,
                    normalized_quote=normalized_quote,
                    matched_keywords=matched_keywords,
                    identity_matches=identity_matches,
                    page_number=page.page_number,
                    scope_warning=scope_warning,
                )

                candidates.append(
                    GroundingSnippet(
                        snippet_id=(
                            f"{record.source_id}:{page.page_number or 0}:{len(candidates) + 1}"
                        ),
                        source_id=record.source_id,
                        url=HttpUrl(record.url),
                        title=record.title,
                        document_type=document_type,
                        retrieved_at=_source_datetime(record),
                        page=page.page_number,
                        quote=quote,
                        score=score,
                        matched_keywords=matched_keywords,
                        fund_identity_matches=identity_matches,
                        scope_warning=scope_warning,
                    )
                )

    ranked = sorted(
        candidates,
        key=lambda snippet: (
            snippet.scope_warning,
            -snippet.score,
            snippet.source_id,
            snippet.page or 0,
        ),
    )

    return ranked[:max_snippets]


def _snippet_score(
    *,
    field: FallbackField,
    document_type: DocumentType,
    normalized_quote: str,
    matched_keywords: list[str],
    identity_matches: int,
    page_number: int | None,
    scope_warning: bool,
) -> int:
    score = DOCUMENT_PRIORITY[field].get(
        document_type,
        10,
    )

    score += min(
        len(matched_keywords) * 25,
        100,
    )

    score += min(
        identity_matches * 10,
        40,
    )

    if page_number is not None:
        score += 5

    if field is FallbackField.TARGET_RETURN:
        if PERCENT_PATTERN.search(normalized_quote):
            score += 25

        if any(
            marker in normalized_quote
            for marker in (
                "p a",
                "per annum",
                "rocne",
                "annual",
            )
        ):
            score += 10

    elif field is FallbackField.MINIMUM_INVESTMENT:
        if MONEY_PATTERN.search(normalized_quote):
            score += 25

    elif field is FallbackField.FEES:
        if PERCENT_PATTERN.search(normalized_quote):
            score += 25

    elif field is FallbackField.ASSETS_UNDER_MANAGEMENT:
        if MONEY_PATTERN.search(normalized_quote):
            score += 20

        if DATE_PATTERN.search(normalized_quote):
            score += 25

    elif field is FallbackField.INVESTMENT_HORIZON:
        if re.search(
            r"\b\d+(?:[,.]\d+)?\s*(?:let|rok|roky|years?)\b",
            normalized_quote,
        ):
            score += 25

    if scope_warning:
        score = max(
            score - 80,
            0,
        )

    return score


def _field_status(
    output: FundOutput,
    field: FallbackField,
) -> FieldStatus:
    match field:
        case FallbackField.INVESTMENT_HORIZON:
            return output.investment_horizon.status

        case FallbackField.MINIMUM_INVESTMENT:
            return output.minimum_investment.status

        case FallbackField.TARGET_RETURN:
            return output.target_return.status

        case FallbackField.FEES:
            return output.fees.status

        case FallbackField.ASSETS_UNDER_MANAGEMENT:
            return output.assets_under_management.status

    raise AssertionError(f"Unsupported grounding field: {field}")


def _field_reason(
    output: FundOutput,
    field: FallbackField,
) -> str | None:
    match field:
        case FallbackField.INVESTMENT_HORIZON:
            reason = output.investment_horizon.reason

        case FallbackField.MINIMUM_INVESTMENT:
            reason = output.minimum_investment.reason

        case FallbackField.TARGET_RETURN:
            reason = output.target_return.reason

        case FallbackField.FEES:
            reason = output.fees.reason

        case FallbackField.ASSETS_UNDER_MANAGEMENT:
            reason = output.assets_under_management.reason

        case _:
            raise AssertionError(f"Unsupported grounding field: {field}")

    if reason is None:
        return None

    return f"{reason.code.value}: {reason.detail}"


def _field_instructions(
    field: FallbackField,
) -> list[str]:
    common = [
        (
            "Use only the supplied snippets. Do not use external "
            "knowledge or infer an unstated value."
        ),
        (
            "Return no value when the evidence is missing, ambiguous "
            "or refers to a manager, group or different fund."
        ),
        ("Every accepted value must cite a snippet_id and preserve the source page number."),
        (
            "Do not combine values from different share classes, "
            "subfunds or reporting dates without explicitly identifying "
            "the distinction."
        ),
    ]

    match field:
        case FallbackField.INVESTMENT_HORIZON:
            specific = "Extract only an explicitly stated recommended or minimum investment period."

        case FallbackField.MINIMUM_INVESTMENT:
            specific = (
                "Distinguish the fund subscription minimum from a "
                "general legal threshold for qualified investors."
            )

        case FallbackField.TARGET_RETURN:
            specific = (
                "Do not use historical performance as a substitute for target or expected return."
            )

        case FallbackField.FEES:
            specific = (
                "Keep fee type, percentage or amount, frequency, "
                "basis, maximum flag and share class separate."
            )

        case FallbackField.ASSETS_UNDER_MANAGEMENT:
            specific = (
                "Accept only fund-level assets with an explicit amount, "
                "currency and reporting date."
            )

        case _:
            raise AssertionError(f"Unsupported grounding field: {field}")

    return [
        *common,
        specific,
    ]


def _fund_identity_tokens(
    fund_name: str,
) -> tuple[str, ...]:
    normalized = normalize_search_text(fund_name)

    tokens = re.findall(
        r"[a-z0-9]+",
        normalized,
    )

    result: list[str] = []

    for token in tokens:
        if token in FUND_NAME_NOISE_TOKENS:
            continue

        if len(token) < 2:
            continue

        if token not in result:
            result.append(token)

    return tuple(result)


def _document_type(
    raw_document_type: str | None,
) -> DocumentType:
    if raw_document_type is None:
        return DocumentType.OTHER

    try:
        return DocumentType(raw_document_type)
    except ValueError:
        return DocumentType.OTHER


def _source_datetime(
    record: ParsedDocumentRecord,
) -> datetime:
    raw_value = record.retrieved_at or record.parsed_at

    parsed = datetime.fromisoformat(raw_value)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed
