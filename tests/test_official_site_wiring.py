"""The official-website register reaching a real extraction run.

`official_site_owns_page` and `build_official_site_index` are unit-tested next to the other
attribution rules. What these tests cover is the wiring: that an index handed to
`extract_fund_data` decides identity inside the extraction, that the two-pass service builds one
from the canonical input on its own, and that leaving it out changes nothing.
"""

from __future__ import annotations

from pathlib import Path

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
from fundscraper.extraction_service import extract_fund_data
from fundscraper.field_extraction import build_official_site_index
from fundscraper.models import FundInput
from fundscraper.output_models import FieldStatus
from fundscraper.output_service import create_pending_output, stable_fund_id, write_output

FUND = FundInput(
    name="Lázeňský fond SICAV a.s.",
    web="https://lazenskyfond.cz",
)

# A fund of the same house whose website is a page on a shared hub.
HUB_FUND = FundInput(
    name="CARE SICAV, a.s.",
    web="https://www.codyainvest.cz/nase-fondy/care-sicav-a-s",
)


# The homepage as the site publishes it: the figures first, then a
# marketing line that declines the fund's own name into a form the
# mention parser reads as a different fund.
HOMEPAGE = "\n".join(
    (
        "Fondovy kapital: 693 601 745 Kc k 31.12.2025",
        "Tradice, stability, zdravi. Investujte do lazenskeho fondu SICAV a.s.",
    )
)


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    database_path = tmp_path / "run.sqlite3"

    output_path = tmp_path / "funds.enriched.json"

    initialize_database(database_path)

    funds = [FUND, HUB_FUND]

    register_funds(database_path, funds)

    write_output(output_path, create_pending_output(funds))

    fund_id = stable_fund_id(FUND)

    document = ParsedDocument(
        document_format=DocumentFormat.HTML,
        parser_name="selectolax",
        pages=(ParsedPage(page_number=None, text=HOMEPAGE, character_count=len(HOMEPAGE)),),
        character_count=len(HOMEPAGE),
        scanned_candidate=False,
    )

    body_path = tmp_path / "homepage.html"

    body_path.write_text(HOMEPAGE, encoding="utf-8")

    upsert_source(
        database_path,
        fund_id=fund_id,
        url="https://lazenskyfond.cz/",
        status=SourceStatus.DOWNLOADED,
        document_type="marketing_page",
        content_type="text/html",
        local_path=str(body_path),
    )

    source_id = 1

    text_path = write_parsed_document(
        directory=tmp_path / "parsed",
        fund_id=fund_id,
        source_id=source_id,
        document=document,
    )

    record_parsed_document(
        database_path,
        source_id=source_id,
        fund_id=fund_id,
        document_format=document.document_format.value,
        parser_name=document.parser_name,
        page_count=document.page_count,
        character_count=document.character_count,
        scanned_candidate=False,
        text_path=str(text_path),
    )

    return (database_path, output_path)


def test_register_lets_the_extraction_use_the_funds_own_homepage(tmp_path: Path) -> None:
    database_path, output_path = _prepare(tmp_path)

    summary = extract_fund_data(
        database_path=database_path,
        output_path=output_path,
        fund=FUND,
        official_site=build_official_site_index([FUND, HUB_FUND]),
    )

    assets = dict(summary.field_statuses)["assets_under_management"]

    assert assets is FieldStatus.FOUND


def test_without_the_register_the_same_run_refuses_the_page(tmp_path: Path) -> None:
    database_path, output_path = _prepare(tmp_path)

    summary = extract_fund_data(
        database_path=database_path,
        output_path=output_path,
        fund=FUND,
    )

    assets = dict(summary.field_statuses)["assets_under_management"]

    assert assets is not FieldStatus.FOUND


def test_two_pass_builds_the_register_from_the_canonical_input() -> None:
    """The two-pass service holds the canonical list, so nothing has to pass it one."""

    import inspect

    from fundscraper import two_pass_service

    source = inspect.getsource(two_pass_service.run_two_pass)

    assert "build_official_site_index(canonical_funds)" in source
