"""
Draw a manual QA sample from the safe delivery output.

The delivery file says what a value is; it deliberately says nothing
about where it came from. That is right for the people who consume it
and useless for anyone checking whether it is true. This script pairs
every sampled value back with the evidence the internal output kept —
the document, its type, the quoted sentence, the page — so a reviewer can
open the source and decide.

The sample is stratified across the eleven delivered fields and weighted
towards the values that are hardest to trust: a rate read from a
marketing page with low confidence is worth a reviewer's time in a way
that a manager name read from a statute is not. It is also spread across
funds, source types and document types, so the result measures the
output rather than one fund's website.

Selection is deterministic. The same inputs and the same seed produce the
same sample, so two reviewers can talk about row 47.

Nothing is invented. Where the internal output kept no evidence for a
delivered value, the row says so in ``evidence_status`` and the quote is
null.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fundscraper.delivery_export import (  # noqa: E402
    DELIVERY_FIELDS,
    audit_mismatch_reason,
    input_digest,
    load_audit,
    load_records,
)
from fundscraper.output_audit import classify_source  # noqa: E402

DEFAULT_SIZE: Final = 100

DEFAULT_SEED: Final = 20260812


# The five groups the sample is balanced across, each getting an equal
# share. Without this the sample would be four fifths manager names and
# investment horizons, because those are the fields the crawler finds.
FIELD_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    ("investment_horizon", "minimum_investment"),
    ("target_return", "fees"),
    ("manager", "administrator"),
    ("assets_under_management", "aum_history"),
    ("annual_returns", "historical_values", "news"),
)


# Every delivered field appears in exactly one group, or the sample would
# silently omit one.
assert {name for group in FIELD_GROUPS for name in group} == set(DELIVERY_FIELDS)


# Documents that state a fund's terms, against documents that advertise
# them. A number on a marketing page is not wrong by nature, but it is
# the one worth reading twice.
WEAK_DOCUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "marketing_page",
        "other",
        "infoletter",
        "register",
    }
)


# How many rows one fund may contribute. A sample that is a third one
# manager's funds measures that manager.
MAXIMUM_ROWS_PER_FUND: Final = 3


# The share of each field's quota given to its hardest values. The rest
# is spent on covering document types and funds the hard rows missed, so
# the sample is not only the worst corner of the output.
HARD_SHARE: Final = 0.45


@dataclass(frozen=True, slots=True)
class SampleRow:
    """One delivered value, with what a reviewer needs to judge it."""

    fund_id: str
    fund_name: str
    field: str
    delivered_value: Any
    source_url: str | None
    source_document_type: str | None
    source_title: str | None
    source_type: str
    evidence_quote: str | None
    evidence_status: str
    page: int | None
    section: str | None
    scope: str | None
    extraction_method: str | None
    confidence: str | None
    review_required: bool | None
    audit_status: str
    audit_reason_codes: list[str] = dataclass_field(default_factory=list)
    difficulty: int = 0
    manual_verdict: str = ""
    manual_note: str = ""


def evidence_of(
    result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """
    Return the source block, its metadata and whether evidence survived.

    A delivered value with no stored quote is not a defect of this
    script and must not be presented as one, so the row keeps the value
    and names what is missing.
    """

    source = result.get("source")

    if not isinstance(source, dict):
        return (
            {},
            {},
            "no_source_stored",
        )

    metadata = source.get("source")

    metadata = metadata if isinstance(metadata, dict) else {}

    quote = source.get("quote")

    if not quote:
        return (
            source,
            metadata,
            "no_quote_stored",
        )

    return (
        source,
        metadata,
        "quote_recovered",
    )


def structured_size(
    value: Any,
) -> int:
    """Return how many parts a delivered value has."""

    if not isinstance(value, dict):
        return 1

    for key in ("items", "observations", "series"):
        part = value.get(key)

        if isinstance(part, list):
            return max(len(part), 1)

    return 1


def difficulty_of(
    *,
    result: dict[str, Any],
    document_type: str | None,
    source_type: str,
    evidence_status: str,
    value: Any,
    fund_findings: int,
) -> int:
    """
    Score how much a delivered value needs a human to look at it.

    Every part of this is a property of the evidence, not of the number.
    A value is hard to check when the document is promotional, the parser
    was unsure, the source is not the fund's own site, or the value has
    many parts that each had to be read correctly.
    """

    score = 0

    extraction = result.get("extraction")

    extraction = extraction if isinstance(extraction, dict) else {}

    if extraction.get("review_required"):
        score += 3

    confidence = extraction.get("confidence")

    if confidence == "low":
        score += 3
    elif confidence == "medium":
        score += 1

    if source_type != "official_website":
        score += 2

    if document_type in WEAK_DOCUMENT_TYPES:
        score += 2

    if evidence_status != "quote_recovered":
        score += 4

    parts = structured_size(value)

    if parts > 3:
        score += 2
    elif parts > 1:
        score += 1

    # The audit doubted something else about this fund. Not proof of
    # anything here, but a fund whose other fields were wrong is worth a
    # closer look.
    if fund_findings:
        score += 1

    scope = result.get("scope")

    if isinstance(scope, dict) and scope.get("type") not in ("fund", "subfund"):
        score += 2

    return score


def build_rows(
    *,
    delivered: list[dict[str, Any]],
    internal: list[dict[str, Any]],
    audit_findings: list[dict[str, Any]],
) -> list[SampleRow]:
    """Pair every delivered value with the evidence behind it."""

    by_name = {str(record.get("name") or ""): record for record in internal}

    findings_by_fund: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for finding in audit_findings:
        findings_by_fund[str(finding.get("fund_id") or "")].append(finding)

    rows: list[SampleRow] = []

    for record in delivered:
        name = str(record.get("name") or "")

        source_record = by_name.get(name)

        if source_record is None:
            continue

        fund_id = str(source_record.get("fund_id") or "")

        fund_web = source_record.get("web")

        fund_findings = findings_by_fund.get(fund_id, [])

        for field_name in DELIVERY_FIELDS:
            delivered_field = record.get(field_name)

            if not isinstance(delivered_field, dict):
                continue

            if delivered_field.get("status") != "found":
                continue

            result = source_record.get(field_name)

            result = result if isinstance(result, dict) else {}

            source, metadata, evidence_status = evidence_of(result)

            url = metadata.get("url")

            source_type = classify_source(
                source_url=url,
                fund_web=fund_web,
            ).value

            document_type = metadata.get("document_type")

            field_findings = [
                finding for finding in fund_findings if finding.get("field") == field_name
            ]

            scope = result.get("scope")

            extraction = result.get("extraction")

            extraction = extraction if isinstance(extraction, dict) else {}

            rows.append(
                SampleRow(
                    fund_id=fund_id,
                    fund_name=name,
                    field=field_name,
                    delivered_value=delivered_field.get("value"),
                    source_url=url,
                    source_document_type=document_type,
                    source_title=metadata.get("title"),
                    source_type=source_type,
                    evidence_quote=source.get("quote"),
                    evidence_status=evidence_status,
                    page=source.get("page"),
                    section=source.get("section"),
                    scope=(scope.get("type") if isinstance(scope, dict) else None),
                    extraction_method=extraction.get("method"),
                    confidence=extraction.get("confidence"),
                    review_required=extraction.get("review_required"),
                    # A delivered field is one the audit did not refuse.
                    # A finding here is one it raised without blocking.
                    audit_status=("valid" if not field_findings else "valid_with_notes"),
                    audit_reason_codes=sorted(
                        {str(finding.get("reason_code") or "") for finding in field_findings}
                    ),
                    difficulty=difficulty_of(
                        result=result,
                        document_type=document_type,
                        source_type=source_type,
                        evidence_status=evidence_status,
                        value=delivered_field.get("value"),
                        fund_findings=len(fund_findings),
                    ),
                )
            )

    return rows


def field_quotas(
    *,
    available: Counter[str],
    size: int,
) -> dict[str, int]:
    """Split the sample size across the groups, then across their fields."""

    quotas: dict[str, int] = {}

    per_group = size // len(FIELD_GROUPS)

    for index, group in enumerate(FIELD_GROUPS):
        # The last group absorbs the remainder, so the quotas add up.
        budget = per_group if index < len(FIELD_GROUPS) - 1 else size - per_group * index

        present = [name for name in group if available[name]]

        if not present:
            continue

        total = sum(available[name] for name in present)

        # A floor, so a field with six delivered values is still visible
        # next to one with a hundred.
        floor = max(1, budget // (len(present) * 2))

        assigned = {
            name: min(
                available[name],
                max(floor, round(budget * available[name] / total)),
            )
            for name in present
        }

        # Rounding and the floor both overshoot; give back from the
        # field with the most left over, never below its floor.
        while sum(assigned.values()) > budget:
            candidate = max(
                (name for name in present if assigned[name] > 1),
                key=lambda name: (assigned[name], name),
                default=None,
            )

            if candidate is None:
                break

            assigned[candidate] -= 1

        while sum(assigned.values()) < budget:
            candidate = min(
                (name for name in present if assigned[name] < available[name]),
                key=lambda name: (assigned[name], name),
                default=None,
            )

            if candidate is None:
                break

            assigned[candidate] += 1

        quotas.update(assigned)

    return quotas


def select(
    *,
    rows: list[SampleRow],
    size: int,
    seed: int,
) -> list[SampleRow]:
    """
    Choose the sample: the hardest values first, then the widest spread.

    Two passes with different aims. The first takes, per field, the rows
    a reviewer is most likely to find something in — promotional
    sources, low confidence, values the parser itself flagged.

    Left alone that pass produces a sample of marketing pages, because
    that is what a hard row looks like, and a reviewer would learn
    nothing about whether the statutes and key information documents
    were read correctly. So the second pass fills the remaining quota by
    repeatedly taking the document type furthest below its share of the
    delivered output. The result is weighted towards the doubtful
    without abandoning the ordinary.
    """

    shuffler = random.Random(seed)

    by_field: dict[str, list[SampleRow]] = defaultdict(list)

    for row in rows:
        by_field[row.field].append(row)

    available = Counter({name: len(items) for name, items in by_field.items()})

    quotas = field_quotas(
        available=available,
        size=size,
    )

    # What each document type is worth in the delivered output, so the
    # fill pass knows what it is short of.
    population_share = {
        document_type: count / len(rows)
        for document_type, count in Counter(str(row.source_document_type) for row in rows).items()
    }

    chosen: list[SampleRow] = []

    taken_ids: set[int] = set()

    per_fund: Counter[str] = Counter()

    per_field: Counter[str] = Counter()

    def accept(row: SampleRow) -> None:
        chosen.append(row)

        taken_ids.add(id(row))

        per_fund[row.fund_id] += 1

        per_field[row.field] += 1

    # Pass one: the hardest rows of each field.
    for field_name, quota in sorted(quotas.items()):
        candidates = list(by_field[field_name])

        # Shuffle first so equal-difficulty rows are not taken in file
        # order, then sort: a deterministic shuffle within each level.
        shuffler.shuffle(candidates)

        candidates.sort(key=lambda row: -row.difficulty)

        hard_target = round(quota * HARD_SHARE)

        for row in candidates:
            if per_field[field_name] >= hard_target:
                break

            if per_fund[row.fund_id] >= MAXIMUM_ROWS_PER_FUND:
                continue

            accept(row)

    # Pass two: fill the rest, always from the document type the sample
    # is furthest short of.
    while len(chosen) < size:
        counts = Counter(str(row.source_document_type) for row in chosen)

        def deficit(
            row: SampleRow,
            counts: Counter[str] = counts,
        ) -> float:
            """Return how far the sample is below this type's share so far."""

            document_type = str(row.source_document_type)

            target = population_share.get(document_type, 0.0) * size

            return target - counts[document_type]

        eligible = [
            row
            for row in rows
            if id(row) not in taken_ids
            and per_field[row.field] < quotas.get(row.field, 0)
            and per_fund[row.fund_id] < MAXIMUM_ROWS_PER_FUND
        ]

        if not eligible:
            break

        eligible.sort(
            key=lambda row: (
                -deficit(row),
                -row.difficulty,
                row.fund_id,
                row.field,
            )
        )

        accept(eligible[0])

    chosen.sort(key=lambda row: (row.field, row.fund_name))

    return chosen


def write_json(
    *,
    rows: list[SampleRow],
    path: Path,
    provenance: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            {
                **provenance,
                "sample_size": len(rows),
                "rows": [asdict(row) for row in rows],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


CSV_COLUMNS: Final[tuple[str, ...]] = (
    "fund_id",
    "fund_name",
    "field",
    "delivered_value",
    "source_url",
    "source_document_type",
    "source_title",
    "source_type",
    "evidence_quote",
    "evidence_status",
    "page",
    "section",
    "scope",
    "extraction_method",
    "confidence",
    "review_required",
    "audit_status",
    "audit_reason_codes",
    "difficulty",
    "manual_verdict",
    "manual_note",
)


def write_csv(
    *,
    rows: list[SampleRow],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    # utf-8-sig so Excel opens the Czech quotes correctly, and \r\n so a
    # quote spanning lines stays inside its cell.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=CSV_COLUMNS,
            lineterminator="\r\n",
        )

        writer.writeheader()

        for row in rows:
            record = asdict(row)

            value = record["delivered_value"]

            record["delivered_value"] = (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, dict | list)
                else str(value)
            )

            record["audit_reason_codes"] = ", ".join(record["audit_reason_codes"])

            writer.writerow({column: record.get(column, "") for column in CSV_COLUMNS})


def report(
    *,
    rows: list[SampleRow],
    population: list[SampleRow],
) -> None:
    print("=" * 72)
    print(f"MANUAL QA SAMPLE — {len(rows)} values drawn from {len(population)} delivered")
    print("=" * 72)

    print()
    print("BY FIELD")
    print("-" * 72)

    sampled = Counter(row.field for row in rows)

    whole = Counter(row.field for row in population)

    for name in DELIVERY_FIELDS:
        if not whole[name]:
            continue

        share = 100 * sampled[name] / whole[name]

        print(f"  {name:26s}{sampled[name]:>5} of {whole[name]:>4} delivered  ({share:4.0f} %)")

    print()
    print("BY SOURCE TYPE")
    print("-" * 72)

    for source_type, count in Counter(row.source_type for row in rows).most_common():
        print(f"  {source_type:26s}{count:>5}")

    print()
    print("BY DOCUMENT TYPE")
    print("-" * 72)

    for document_type, count in Counter(
        str(row.source_document_type) for row in rows
    ).most_common():
        print(f"  {document_type:26s}{count:>5}")

    print()
    print("EVIDENCE")
    print("-" * 72)

    for status, count in Counter(row.evidence_status for row in rows).most_common():
        print(f"  {status:26s}{count:>5}")

    with_page = sum(1 for row in rows if row.page)

    print(f"  {'with a page number':26s}{with_page:>5}")

    print()
    print("HOW HARD THE SAMPLE IS")
    print("-" * 72)

    print(f"  {'review_required':26s}{sum(1 for row in rows if row.review_required):>5}")

    for level in ("low", "medium", "high"):
        count = sum(1 for row in rows if row.confidence == level)

        print(f"  {'confidence ' + level:26s}{count:>5}")

    print(f"  {'distinct funds':26s}{len({row.fund_id for row in rows}):>5}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="build_manual_qa_sample",
        description=(
            "Draw a stratified manual QA sample from the safe delivery "
            "output, paired with the evidence of the internal output."
        ),
    )

    parser.add_argument(
        "--delivery",
        type=Path,
        default=Path("data/output/funds.delivery.safe.json"),
        help="Safe delivery output to sample.",
    )
    parser.add_argument(
        "--internal",
        type=Path,
        default=Path("data/output/funds.after-fast.json"),
        help="Internal auditable output the delivery was derived from.",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=Path("reports/output-audit.after-fast.json"),
        help="Audit report of that internal output.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=Path("reports/manual-qa-sample.json"),
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        type=Path,
        default=Path("reports/manual-qa-sample.csv"),
    )
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)

    arguments = parser.parse_args()

    delivered = load_records(arguments.delivery)

    internal = load_records(arguments.internal)

    audit = load_audit(arguments.audit)

    # The same check the export makes. A sample built from an audit of
    # another file would label rows with verdicts that were never about
    # them.
    mismatch = audit_mismatch_reason(
        audit=audit,
        records=internal,
        input_sha256=input_digest(arguments.internal),
    )

    if mismatch is not None:
        print(
            f"The audit report does not describe {arguments.internal}: {mismatch}.",
            file=sys.stderr,
        )

        return 1

    population = build_rows(
        delivered=delivered,
        internal=internal,
        audit_findings=list(audit.findings),
    )

    rows = select(
        rows=population,
        size=arguments.size,
        seed=arguments.seed,
    )

    provenance = {
        "delivery_path": str(arguments.delivery),
        "internal_path": str(arguments.internal),
        "audit_path": str(arguments.audit),
        "input_sha256": audit.input_sha256,
        "ruleset_version": audit.ruleset_version,
        "seed": arguments.seed,
        "delivered_values": len(population),
    }

    write_json(
        rows=rows,
        path=arguments.json_path,
        provenance=provenance,
    )

    write_csv(
        rows=rows,
        path=arguments.csv_path,
    )

    report(
        rows=rows,
        population=population,
    )

    print()
    print(f"JSON: {arguments.json_path}")
    print(f"CSV:  {arguments.csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
