"""Fund-name identity tokens must be the same in both consumers.

`field_extraction.py` and `grounding_packets.py` each answer "which words of this
fund's registered name identify it". The two kept separate copies of the noise
list, and the SICAV fix - dropping "zakladnim" from the legal form
"s promennym zakladnim kapitalem" - reached only the primary parser. Both now
read `fundscraper.fund_identity`; these tests pin that one answer, and the
grounded case below pins that the review path really goes through it.

Fund names are verbatim from `data/input/funds.json`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fundscraper.database import (
    SourceStatus,
    initialize_database,
    record_parsed_document,
    register_funds,
    upsert_source,
)
from fundscraper.document_parser import (
    DocumentFormat,
    ParsedDocument,
    ParsedPage,
    write_parsed_document,
)
from fundscraper.fallback_sources import (
    FallbackField,
)
from fundscraper.field_extraction import (
    extract_fund_fields,
)
from fundscraper.fund_identity import (
    fund_identity_tokens,
)
from fundscraper.grounding_packets import (
    build_grounding_packets,
)
from fundscraper.models import FundInput
from fundscraper.output_models import (
    ProcessingMetadata,
    ProcessingStatus,
)
from fundscraper.output_service import (
    create_pending_output,
    stable_fund_id,
)

# The registered name of 29 canonical funds carries the SICAV legal form
# "s promennym zakladnim kapitalem". None of its three words tells one of those
# funds from another.
SICAV_FUND_NAME = "Nemomax investiční fond s proměnným základním kapitálem, a.s."

OTHER_SICAV_FUND_NAME = "PRAGORENT investiční fond s proměnným základním kapitálem, a.s."

# Two real funds whose distinctive part is a single short word, guarding the
# opposite failure: stripping so much that a fund has no identity left. They are
# also the pair that rules out one shared noise list across every consumer - the
# Czech fund needs "czech" treated as noise, this one needs it kept, and a union
# of the two would leave both funds with no tokens at all.
SHORT_FUND_NAMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Český fond SICAV, a.s.",
        ("cesky",),
    ),
    (
        "Czech Investment Fund SICAV, a.s.",
        ("czech",),
    ),
)

# Pinned rather than `datetime.now(UTC)`: nothing here should read the clock.
RUN_TIME = datetime(
    2026,
    7,
    23,
    12,
    0,
    tzinfo=UTC,
)


def test_sicav_legal_form_contributes_no_identity_token() -> None:
    assert fund_identity_tokens(SICAV_FUND_NAME) == ("nemomax",)


@pytest.mark.parametrize(
    (
        "fund_name",
        "expected",
    ),
    SHORT_FUND_NAMES,
    ids=[fund_name for fund_name, _ in SHORT_FUND_NAMES],
)
def test_short_fund_name_keeps_its_only_distinctive_token(
    fund_name: str,
    expected: tuple[str, ...],
) -> None:
    """The other side of the rule: noise stripping must not empty a fund out."""
    assert fund_identity_tokens(fund_name) == expected


def test_grounded_snippet_earns_no_identity_credit_from_the_shared_legal_form(
    tmp_path: Path,
) -> None:
    """A document about a different SICAV must not read as evidence of this fund.

    `_collect_field_snippets` credits a snippet for every identity token found in
    the document. While "zakladnim" counted as identity, the legal form shared by
    29 funds scored as if it named one of them.
    """
    fund = FundInput(
        name=SICAV_FUND_NAME,
        web="https://nemomax.example/fond",
    )

    fund_id = stable_fund_id(fund)

    database_path = tmp_path / "fundscraper.sqlite3"

    parsed_directory = tmp_path / "parsed"

    initialize_database(database_path)

    register_funds(
        database_path,
        [
            fund,
        ],
    )

    source_id = upsert_source(
        database_path,
        fund_id=fund_id,
        url="https://spravce.example/statut-pragorent.pdf",
        status=SourceStatus.DOWNLOADED,
        document_type="memorandum",
        content_type="application/pdf",
        title=f"{OTHER_SICAV_FUND_NAME} - investicni memorandum",
        retrieved_at=RUN_TIME,
        http_status=200,
        sha256="b" * 64,
        local_path="cache/http/pragorent.body",
    )

    text = f"""
    {OTHER_SICAV_FUND_NAME}

    Investicni strategie fondu je zamerena na dlouhodoby rust.

    Ocekavany vynos fondu se muze pohybovat kolem 8 % p.a.,
    ale tato hodnota neni garantovana.
    """.strip()

    parsed_document = ParsedDocument(
        document_format=DocumentFormat.PDF,
        parser_name="pymupdf",
        pages=(
            ParsedPage(
                page_number=1,
                text=text,
                character_count=len(text),
            ),
        ),
        character_count=len(text),
        scanned_candidate=False,
    )

    parsed_path = write_parsed_document(
        directory=parsed_directory,
        fund_id=fund_id,
        source_id=source_id,
        document=parsed_document,
    )

    record_parsed_document(
        database_path,
        source_id=source_id,
        fund_id=fund_id,
        document_format="pdf",
        parser_name="pymupdf",
        page_count=1,
        character_count=len(text),
        scanned_candidate=False,
        text_path=str(parsed_path),
    )

    outputs = create_pending_output(
        [
            fund,
        ]
    )

    missing_fields = extract_fund_fields(
        fund_name=fund.name,
        documents=[],
    )

    outputs[0] = outputs[0].model_copy(
        update={
            "investment_horizon": (missing_fields.investment_horizon),
            "minimum_investment": (missing_fields.minimum_investment),
            "target_return": (missing_fields.target_return),
            "fees": missing_fields.fees,
            "assets_under_management": (missing_fields.assets_under_management),
            "processing": ProcessingMetadata(
                status=ProcessingStatus.PARTIAL,
                updated_at=RUN_TIME,
            ),
        }
    )

    report = build_grounding_packets(
        funds=[
            fund,
        ],
        outputs=outputs,
        database_path=database_path,
        max_snippets=5,
        now=RUN_TIME,
    )

    target_packet = next(
        packet for packet in report.packets if (packet.field is FallbackField.TARGET_RETURN)
    )

    assert len(target_packet.snippets) == 1

    snippet = target_packet.snippets[0]

    assert "Ocekavany vynos" in snippet.quote

    assert snippet.fund_identity_matches == 0
