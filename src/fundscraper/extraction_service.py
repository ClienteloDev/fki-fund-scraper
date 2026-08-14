from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fundscraper.conflict_resolution import ConflictLedger
from fundscraper.database import (
    AttemptStatus,
    DatabaseError,
    list_parsed_documents,
    record_attempt,
)
from fundscraper.document_parser import (
    DocumentParseError,
    load_parsed_document,
)
from fundscraper.extended_extraction import (
    ExtendedFundFields,
    extract_extended_fields,
)
from fundscraper.extended_persistence import (
    persist_extended_fields,
)
from fundscraper.field_extraction import (
    ExtractionDocument,
    IsinIdentityIndex,
    OfficialSiteIndex,
    extract_fund_fields,
    official_isin_identity,
    official_site_identity,
)
from fundscraper.models import FundInput
from fundscraper.output_models import (
    FieldStatus,
    ProcessingMetadata,
    ProcessingStatus,
)
from fundscraper.output_service import (
    OutputFileError,
    load_output,
    stable_fund_id,
    write_output,
)


class ExtractionServiceError(RuntimeError):
    """Raised when fund field extraction cannot be completed."""


@dataclass(frozen=True, slots=True)
class ExtractionSummary:
    fund_id: str
    fund_name: str
    parsed_documents: int
    fields_found: int
    fields_missing: int
    field_statuses: tuple[
        tuple[
            str,
            FieldStatus,
        ],
        ...,
    ]
    warnings: tuple[str, ...]
    output_path: Path

    # The fields added in schema version 3 are reported next to the
    # delivered ones instead of inside them, so an existing consumer of
    # this summary keeps reading the same five numbers.
    extended_statuses: tuple[
        tuple[
            str,
            FieldStatus,
        ],
        ...,
    ] = ()
    extended_rows: int = 0

    # How many groups of candidates disagreed about the same thing while
    # this fund was extracted. Zero for a caller that passes no ledger.
    conflicts_inspected: int = 0


def extract_fund_data(
    *,
    database_path: Path,
    output_path: Path,
    fund: FundInput,
    ledger: ConflictLedger | None = None,
    isin_identity: IsinIdentityIndex | None = None,
    official_site: OfficialSiteIndex | None = None,
) -> ExtractionSummary:
    """
    Extract supported fields and update one fund in output JSON.

    ``ledger`` collects every disagreement the extraction had to decide.
    It is optional because the delivered output does not depend on it:
    the winner and its losing alternatives reach the file either way, and
    the ledger is what the conflict report is written from.

    ``official_site`` is the register of hosts that are one fund's own
    official website, built from the canonical input. When it is given, a
    page served from this fund's own site is identified as the fund's
    without the page having to repeat its full legal name, which a
    homepage rarely does. Left out, identity is decided exactly as it was
    before the register existed.

    ``isin_identity`` is the official ISIN register. When it is given, a
    document that prints an official ISIN of this fund is identified by
    that ISIN instead of by repeating the fund's legal name, which a
    subfund KID rarely does. Left out, identity is decided exactly as it
    was before the register existed.
    """

    fund_id = stable_fund_id(fund)

    fund_ledger = ledger.for_fund(fund.name) if ledger is not None else None

    conflicts_before = len(ledger.records) if ledger is not None else 0

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="extract_fields",
        status=AttemptStatus.STARTED,
        url=fund.web,
    )

    warnings: list[str] = []

    try:
        records = list_parsed_documents(
            database_path,
            fund_id=fund_id,
        )

        documents: list[ExtractionDocument] = []

        for record in records:
            try:
                parsed_document = load_parsed_document(Path(record.text_path))
            except DocumentParseError as exc:
                warnings.append(f"Source {record.source_id} could not be loaded: {exc}")

                continue

            documents.append(
                ExtractionDocument(
                    record=record,
                    document=parsed_document,
                )
            )

        with official_isin_identity(isin_identity), official_site_identity(official_site):
            extracted = extract_fund_fields(
                fund_name=fund.name,
                documents=documents,
                fund_web=fund.web,
                ledger=fund_ledger,
            )

            extended = extract_extended_fields(
                fund_name=fund.name,
                fund_web=fund.web,
                documents=documents,
                ledger=fund_ledger,
            )

        field_statuses = (
            (
                "investment_horizon",
                extracted.investment_horizon.status,
            ),
            (
                "minimum_investment",
                extracted.minimum_investment.status,
            ),
            (
                "target_return",
                extracted.target_return.status,
            ),
            (
                "fees",
                extracted.fees.status,
            ),
            (
                "assets_under_management",
                extracted.assets_under_management.status,
            ),
        )

        fields_found = sum(1 for _, status in field_statuses if status is FieldStatus.FOUND)

        outputs = load_output(output_path)

        matching_index = next(
            (index for index, item in enumerate(outputs) if item.fund_id == fund_id),
            None,
        )

        if matching_index is None:
            raise ExtractionServiceError(f"Fund does not exist in output file: {fund.name}")

        processing_status = (
            ProcessingStatus.COMPLETED if fields_found == 5 else ProcessingStatus.PARTIAL
        )

        current_output = outputs[matching_index]

        outputs[matching_index] = current_output.model_copy(
            update={
                "investment_horizon": (extracted.investment_horizon),
                "minimum_investment": (extracted.minimum_investment),
                "target_return": (extracted.target_return),
                "fees": extracted.fees,
                "assets_under_management": (extracted.assets_under_management),
                "manager": extended.manager,
                "administrator": extended.administrator,
                "aum_history": extended.aum_history,
                "annual_returns": extended.annual_returns,
                "historical_values": (extended.historical_values),
                "news": extended.news,
                "processing": ProcessingMetadata(
                    status=processing_status,
                    updated_at=datetime.now(UTC),
                    warnings=warnings,
                ),
            }
        )

        write_output(
            output_path,
            outputs,
            overwrite=True,
        )

        persistence = persist_extended_fields(
            database_path=database_path,
            fund_id=fund_id,
            extended=extended,
        )
    except (
        DatabaseError,
        OutputFileError,
        ExtractionServiceError,
    ) as exc:
        record_attempt(
            database_path,
            fund_id=fund_id,
            stage="extract_fields",
            status=AttemptStatus.FAILED,
            url=fund.web,
            error_code="field_extraction_error",
            error_message=str(exc),
        )

        if isinstance(
            exc,
            ExtractionServiceError,
        ):
            raise

        raise ExtractionServiceError(f"Could not extract fund fields: {exc}") from exc

    record_attempt(
        database_path,
        fund_id=fund_id,
        stage="extract_fields",
        status=AttemptStatus.SUCCEEDED,
        url=fund.web,
    )

    return ExtractionSummary(
        fund_id=fund_id,
        fund_name=fund.name,
        parsed_documents=len(documents),
        fields_found=fields_found,
        fields_missing=5 - fields_found,
        field_statuses=field_statuses,
        warnings=tuple(warnings),
        output_path=output_path,
        extended_statuses=extended_field_statuses(extended),
        extended_rows=persistence.total,
        conflicts_inspected=((len(ledger.records) - conflicts_before) if ledger else 0),
    )


def extended_field_statuses(
    extended: ExtendedFundFields,
) -> tuple[
    tuple[
        str,
        FieldStatus,
    ],
    ...,
]:
    """Return the status of every field added in schema version 3."""

    return (
        (
            "manager",
            extended.manager.status,
        ),
        (
            "administrator",
            extended.administrator.status,
        ),
        (
            "aum_history",
            extended.aum_history.status,
        ),
        (
            "annual_returns",
            extended.annual_returns.status,
        ),
        (
            "historical_values",
            extended.historical_values.status,
        ),
        (
            "news",
            extended.news.status,
        ),
    )
