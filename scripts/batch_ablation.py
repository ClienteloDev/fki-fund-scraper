"""
Which of the five fixes actually earned each recovered value.

Comparing two whole generations says how much was gained; it does not say by
what. This does: it re-runs the extraction over the sources a batch already
holds, once with everything active and once per fix with that one fix disabled.
A field that is found with everything on and missing with one fix off was earned
by that fix, and a field that survives every ablation was not earned by any of
them.

Nothing is fetched and nothing is written outside ``reports/``. The extraction
is pure with respect to the parsed corpus, so the ablations are exact rather
than an estimate.

Usage::

    uv run python scripts/batch_ablation.py <batch> [generation]
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[0]

sys.path.insert(0, str(SCRIPT_DIRECTORY))

sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from batch_selection import DELIVERY_FIELDS, resolve  # noqa: E402

import fundscraper.field_extraction as fe  # noqa: E402
import fundscraper.fund_identity as fund_identity  # noqa: E402
from fundscraper.database import ParsedDocumentRecord  # noqa: E402
from fundscraper.document_parser import DocumentFormat, load_parsed_document  # noqa: E402
from fundscraper.extended_extraction import extract_extended_fields  # noqa: E402
from fundscraper.field_extraction import (  # noqa: E402
    ExtractionDocument,
    extract_fund_fields,
)
from fundscraper.input_loader import load_funds  # noqa: E402
from fundscraper.output_models import FieldStatus  # noqa: E402
from fundscraper.output_service import stable_fund_id  # noqa: E402

FIXES = (
    "1_identity_boilerplate_token",
    "3_html_tables",
    "4_scaled_money_minimum",
    "5_fee_tier_clause",
)


# Fix 6 changes which pages are crawled, not how a source is read, so it
# cannot be ablated by re-extracting a corpus that is already in hand. Its
# effect is measured by the difference in what the two crawls acquired.
CRAWL_ONLY_FIXES = ("6_mandatory_information_label",)


# The minimum-investment pattern as it stood before the scale word was added:
# the currency had to follow the number directly. The multiplier group is kept
# but can never capture, so ``parse_money_amount`` still reads the match.
PRE_SCALE_MINIMUM_PATTERN = re.compile(
    rf"""
    (?:
        minimalni
        |
        nejnizsi
        |
        minimum
        |
        pocatecni
        |
        initial
    )
    .{{0,40}}?
    (?:
        investice
        |
        vklad
        |
        upis
        |
        subscription
        |
        investment
    )
    .{{0,100}}?
    (?P<amount>{fe.NUMBER_PATTERN})
    \s*
    (?P<multiplier>)?
    \s*
    (?P<currency>czk|kc|eur|usd)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _rate_forward_only(
    *,
    percentages: list[tuple[int, float]],
    start: int,
    clause: tuple[int, int],
    used: set[int],
) -> float | None:
    """The tier reader as it stood before: the first unused rate after the period."""

    for offset, value in percentages:
        if offset < start or offset in used:
            continue

        used.add(offset)

        return value

    return None


@contextmanager
def _disabled(fix: str | None) -> Iterator[None]:
    """Put one fix back the way it was, for the duration of one extraction."""

    if fix == "1_identity_boilerplate_token":
        # The list lives in ``fundscraper.fund_identity``; ``field_extraction``
        # binds its own name to the same frozenset on import. The tokenizer
        # reads the first, ``iter_named_funds`` and ``_named_fund_matches``
        # read the second, so ablating the token means rebinding both.
        original = fund_identity.FUND_NAME_NOISE_TOKENS

        ablated = frozenset(original - {"zakladnim"})

        fund_identity.FUND_NAME_NOISE_TOKENS = ablated

        fe.FUND_NAME_NOISE_TOKENS = ablated

        try:
            yield
        finally:
            fund_identity.FUND_NAME_NOISE_TOKENS = original

            fe.FUND_NAME_NOISE_TOKENS = original

        return

    if fix == "4_scaled_money_minimum":
        original_pattern = fe.MINIMUM_INVESTMENT_PATTERN

        fe.MINIMUM_INVESTMENT_PATTERN = PRE_SCALE_MINIMUM_PATTERN

        try:
            yield
        finally:
            fe.MINIMUM_INVESTMENT_PATTERN = original_pattern

        return

    if fix == "5_fee_tier_clause":
        original_reader = fe._rate_of_period

        fe._rate_of_period = _rate_forward_only  # type: ignore[assignment]

        try:
            yield
        finally:
            fe._rate_of_period = original_reader  # type: ignore[assignment]

        return

    yield


MARKUP_FORMATS = frozenset(
    {
        DocumentFormat.HTML.value,
        DocumentFormat.XHTML.value,
        DocumentFormat.XML.value,
    }
)


def without_markup_tables(documents: list[ExtractionDocument]) -> list[ExtractionDocument]:
    """Return the same corpus with the grids of every web page removed."""

    stripped: list[ExtractionDocument] = []

    for item in documents:
        if item.document.document_format.value not in MARKUP_FORMATS:
            stripped.append(item)

            continue

        pages = tuple(dataclasses.replace(page, tables=()) for page in item.document.pages)

        stripped.append(
            ExtractionDocument(
                record=item.record,
                document=dataclasses.replace(item.document, pages=pages),
            )
        )

    return stripped


def load_documents(*, database_path: Path, fund_id: str) -> list[ExtractionDocument]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    rows = connection.execute(
        """
        SELECT
            p.source_id, p.fund_id, s.url, s.title, s.document_type, s.content_type,
            s.retrieved_at, p.document_format, p.parser_name, p.page_count,
            p.character_count, p.scanned_candidate, p.text_path, p.parsed_at, s.local_path
        FROM parsed_documents AS p
        JOIN sources AS s ON s.source_id = p.source_id
        WHERE p.fund_id = ?
        ORDER BY p.source_id
        """,
        (fund_id,),
    ).fetchall()

    connection.close()

    documents: list[ExtractionDocument] = []

    for row in rows:
        record = ParsedDocumentRecord(
            source_id=row[0],
            fund_id=row[1],
            url=row[2],
            title=row[3],
            document_type=row[4],
            content_type=row[5],
            retrieved_at=row[6],
            document_format=row[7],
            parser_name=row[8],
            page_count=row[9],
            character_count=row[10],
            scanned_candidate=bool(row[11]),
            text_path=row[12],
            parsed_at=row[13],
            local_path=row[14],
        )

        try:
            parsed = load_parsed_document(Path(record.text_path))
        except Exception:  # noqa: BLE001 - a missing parse is simply not read
            continue

        documents.append(ExtractionDocument(record=record, document=parsed))

    return documents


def found_fields(
    *,
    fund_name: str,
    fund_web: str | None,
    documents: list[ExtractionDocument],
    disabled_fix: str | None,
) -> dict[str, str | None]:
    """Return the source URL of every field found under one configuration."""

    corpus = without_markup_tables(documents) if disabled_fix == "3_html_tables" else documents

    with _disabled(disabled_fix):
        core = extract_fund_fields(
            fund_name=fund_name,
            documents=corpus,
            fund_web=fund_web,
        )

        extended = extract_extended_fields(
            fund_name=fund_name,
            fund_web=fund_web,
            documents=corpus,
        )

    found: dict[str, str | None] = {}

    for field in DELIVERY_FIELDS:
        result = getattr(core, field, None) or getattr(extended, field)

        if result.status is not FieldStatus.FOUND:
            continue

        source = result.source

        holder = getattr(source, "source", None) if source is not None else None

        url = getattr(holder, "url", None) if holder is not None else None

        # The stored evidence carries a validated URL object, which does not
        # survive JSON on its own.
        found[field] = str(url) if url is not None else None

    return found


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    batch_name = sys.argv[1] if len(sys.argv) > 1 else "batch10"

    generation = sys.argv[2] if len(sys.argv) > 2 else "after"

    batch = resolve(batch_name)

    database_path = (
        batch.root
        / generation
        / ("sample8.sqlite3" if batch.name == "sample8" else "batch.sqlite3")
    )

    funds = load_funds(REPOSITORY_ROOT / "data/input/funds.json")

    by_name = {fund.name: fund for fund in funds}

    rows: list[dict[str, object]] = []

    credit: Counter[str] = Counter()

    for name in batch.fund_names:
        fund = by_name[name]

        documents = load_documents(
            database_path=database_path,
            fund_id=stable_fund_id(fund),
        )

        baseline = found_fields(
            fund_name=name,
            fund_web=fund.web,
            documents=documents,
            disabled_fix=None,
        )

        attribution: dict[str, list[str]] = {field: [] for field in baseline}

        for fix in FIXES:
            ablated = found_fields(
                fund_name=name,
                fund_web=fund.web,
                documents=documents,
                disabled_fix=fix,
            )

            for field in baseline:
                if field not in ablated:
                    attribution[field].append(fix)

        for fixes in attribution.values():
            for fix in fixes:
                credit[fix] += 1

        rows.append(
            {
                "fund_name": name,
                "role": batch.role_of(name),
                "parsed_documents": len(documents),
                "found_fields": sorted(baseline),
                "found_count": len(baseline),
                "attribution": {field: fixes for field, fixes in attribution.items() if fixes},
                "unattributed": sorted(field for field, fixes in attribution.items() if not fixes),
                "sources": baseline,
            }
        )

        print(
            f"{name[:46]:48} found={len(baseline):2} "
            f"earned_by_a_fix={sum(1 for f in attribution.values() if f)}",
            flush=True,
        )

    payload = {
        "batch": batch.name,
        "generation": generation,
        "method": (
            "Re-extraction over the corpus the batch already holds, once with every fix "
            "active and once per fix with that one disabled. A field found with all fixes "
            "on and missing with one off was earned by that fix."
        ),
        "credit_by_fix": {fix: credit.get(fix, 0) for fix in FIXES},
        "not_ablatable": {
            fix: "changes which pages are crawled; measured by the acquisition manifests"
            for fix in CRAWL_ONLY_FIXES
        },
        "funds": rows,
    }

    path = REPOSITORY_ROOT / f"reports/{batch.name}-fix-attribution-{generation}.json"

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\ncredit by fix: {payload['credit_by_fix']}")

    print(f"written {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
