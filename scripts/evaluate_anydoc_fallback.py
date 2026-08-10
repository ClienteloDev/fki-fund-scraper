"""
Measure the selective AnyDoc layout fallback on problematic cached PDFs.

The script performs no network access and writes nothing to the processing
database. It picks the cached PDFs whose stored parse trips a layout
trigger, re-reads each one the way the pipeline now would, and reports
what the fallback decided and what the extraction read back either way.

Usage:

    uv run python scripts/evaluate_anydoc_fallback.py \\
        --database cache/regen.sqlite3 \\
        --limit 25 \\
        --report reports/anydoc-fallback-evaluation.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fundscraper.anydoc_fallback import (
    FallbackOutcome,
    LayoutTrigger,
    apply_layout_fallback,
    blocking_reason,
    extracted_field_names,
    layout_triggers,
    measure_layout,
)
from fundscraper.anydoc_parser import anydoc_capabilities
from fundscraper.database import ParsedDocumentRecord
from fundscraper.document_parser import (
    DocumentParseError,
    ParsedDocument,
    load_parsed_document,
    parse_document,
)
from fundscraper.extended_extraction import extract_extended_fields
from fundscraper.field_extraction import ExtractionDocument, extract_fund_fields
from fundscraper.output_models import Confidence, FieldStatus

# The fields the comparison reports, in the order they are printed.
COMPARED_FIELDS = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
    "manager",
    "administrator",
    "aum_history",
    "annual_returns",
    "historical_values",
)


@dataclass(frozen=True, slots=True)
class CachedSource:
    source_id: int
    fund_id: str
    fund_name: str
    fund_web: str | None
    url: str
    title: str | None
    document_type: str | None
    content_type: str | None
    retrieved_at: str | None
    local_path: str
    text_path: str
    parser_name: str
    page_count: int


def load_candidates(
    *,
    database_path: Path,
    scan_limit: int,
) -> list[CachedSource]:
    """
    Read the cached PDFs that are worth testing the fallback against.

    Several funds share the same administrator document, so a checksum is
    only taken once: a sample of the same annual report eight times would
    say nothing about the eight documents it displaced.
    """

    connection = sqlite3.connect(database_path)

    try:
        rows = connection.execute(
            """
            select s.source_id, s.fund_id, f.name, f.web, s.url, s.title,
                   s.document_type, s.content_type, s.retrieved_at, s.local_path,
                   p.text_path, p.parser_name, p.page_count, s.sha256
            from parsed_documents p
            join sources s on s.source_id = p.source_id
            join funds f on f.fund_id = p.fund_id
            where p.document_format = 'pdf'
              and p.scanned_candidate = 0
              and p.character_count > 0
              and s.local_path is not null
            order by p.source_id
            limit ?
            """,
            (scan_limit,),
        ).fetchall()
    finally:
        connection.close()

    seen_checksums: set[str] = set()

    sources: list[CachedSource] = []

    for row in rows:
        checksum = str(row[13] or "")

        if checksum and checksum in seen_checksums:
            continue

        seen_checksums.add(checksum)

        sources.append(
            CachedSource(
                source_id=int(row[0]),
                fund_id=str(row[1]),
                fund_name=str(row[2]),
                fund_web=row[3],
                url=str(row[4]),
                title=row[5],
                document_type=row[6],
                content_type=row[7],
                retrieved_at=row[8],
                local_path=str(row[9]),
                text_path=str(row[10]),
                parser_name=str(row[11] or ""),
                page_count=int(row[12] or 0),
            )
        )

    return sources


def select_problematic(
    *,
    candidates: list[CachedSource],
    limit: int,
) -> tuple[list[tuple[CachedSource, ParsedDocument, tuple[LayoutTrigger, ...]]], int]:
    """
    Return the documents whose stored parse trips a layout trigger.

    The sample is spread over the triggers rather than taken in order, so
    one common layout problem cannot fill it and hide the others. The
    number of documents that tripped a rule is returned beside it, so the
    share of the corpus the fallback would touch can be read off.
    """

    found: dict[
        LayoutTrigger, list[tuple[CachedSource, ParsedDocument, tuple[LayoutTrigger, ...]]]
    ] = {trigger: [] for trigger in LayoutTrigger}

    for source in candidates:
        path = Path(source.text_path)

        if not path.exists():
            continue

        try:
            document = load_parsed_document(path)
        except DocumentParseError:
            continue

        triggers = layout_triggers(document=document)

        if not triggers:
            continue

        found[triggers[0]].append(
            (
                source,
                document,
                triggers,
            )
        )

    selected: list[tuple[CachedSource, ParsedDocument, tuple[LayoutTrigger, ...]]] = []

    # Take one from each trigger in turn until the sample is full, so a
    # rare layout problem is still represented.
    position = 0

    while len(selected) < limit:
        added = False

        for trigger in LayoutTrigger:
            bucket = found[trigger]

            if position >= len(bucket) or len(selected) >= limit:
                continue

            selected.append(bucket[position])

            added = True

        if not added:
            break

        position += 1

    return (
        selected,
        sum(len(bucket) for bucket in found.values()),
    )


def _record_for(
    *,
    source: CachedSource,
    document: ParsedDocument,
) -> ParsedDocumentRecord:
    return ParsedDocumentRecord(
        source_id=source.source_id,
        fund_id=source.fund_id,
        url=source.url,
        title=source.title,
        document_type=source.document_type,
        content_type=source.content_type,
        retrieved_at=source.retrieved_at,
        document_format=document.document_format.value,
        parser_name=document.parser_name,
        page_count=document.page_count,
        character_count=document.character_count,
        scanned_candidate=document.scanned_candidate,
        text_path="",
        parsed_at=datetime.now(UTC).isoformat(),
        local_path=None,
    )


def _field_summary(result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"status": result.status.value}

    if result.status is FieldStatus.FOUND and result.value is not None:
        summary["value"] = result.value.model_dump(mode="json")

        summary["page"] = result.source.page if result.source is not None else None

        if result.extraction is not None:
            summary["confidence"] = result.extraction.confidence.value

            summary["review_required"] = result.extraction.review_required

    elif result.reason is not None:
        summary["reason"] = result.reason.code.value

    return summary


def run_extraction(
    *,
    source: CachedSource,
    document: ParsedDocument,
) -> dict[str, Any]:
    """Run the production extraction over one reading of one document."""

    extraction_document = ExtractionDocument(
        record=_record_for(source=source, document=document),
        document=document,
    )

    core = extract_fund_fields(
        fund_name=source.fund_name,
        documents=[extraction_document],
    )

    extended = extract_extended_fields(
        fund_name=source.fund_name,
        fund_web=source.fund_web,
        documents=[extraction_document],
    )

    results = {
        "investment_horizon": core.investment_horizon,
        "minimum_investment": core.minimum_investment,
        "target_return": core.target_return,
        "fees": core.fees,
        "assets_under_management": core.assets_under_management,
        "manager": extended.manager,
        "administrator": extended.administrator,
        "aum_history": extended.aum_history,
        "annual_returns": extended.annual_returns,
        "historical_values": extended.historical_values,
    }

    fee_items = list(core.fees.value.items) if core.fees.value is not None else []

    summary = {name: _field_summary(result) for name, result in results.items()}

    summary["counts"] = {
        "fields_found": sum(1 for result in results.values() if result.status is FieldStatus.FOUND),
        "fee_items": len(fee_items),
        "fee_tiers": sum(len(item.tiers) for item in fee_items),
        "with_page": sum(
            1
            for result in results.values()
            if result.status is FieldStatus.FOUND
            and result.source is not None
            and result.source.page is not None
        ),
        "review_required": sum(
            1
            for result in results.values()
            if result.status is FieldStatus.FOUND
            and result.extraction is not None
            and result.extraction.review_required
        ),
        "high_confidence": sum(
            1
            for result in results.values()
            if result.status is FieldStatus.FOUND
            and result.extraction is not None
            and result.extraction.confidence is Confidence.HIGH
        ),
    }

    return summary


def compare_extractions(
    *,
    primary: dict[str, Any],
    final: dict[str, Any],
) -> dict[str, Any]:
    """Return how the delivered values differ between the two readings."""

    gained: list[str] = []

    lost: list[str] = []

    changed: list[dict[str, Any]] = []

    for name in COMPARED_FIELDS:
        before = primary.get(name) or {}

        after = final.get(name) or {}

        before_found = before.get("status") == FieldStatus.FOUND.value

        after_found = after.get("status") == FieldStatus.FOUND.value

        if after_found and not before_found:
            gained.append(name)
        elif before_found and not after_found:
            lost.append(name)
        elif before_found and after_found and before.get("value") != after.get("value"):
            changed.append(
                {
                    "field": name,
                    "before": before.get("value"),
                    "after": after.get("value"),
                }
            )

    return {
        "gained": gained,
        "lost": lost,
        "changed": changed,
        "counts_before": primary.get("counts"),
        "counts_after": final.get("counts"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--database", type=Path, default=Path("cache/regen.sqlite3"))

    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/anydoc-fallback-evaluation.json"),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="How many problematic documents to test (20-30 is the intended range).",
    )

    parser.add_argument(
        "--scan-limit",
        type=int,
        default=1200,
        help="How many cached PDFs to inspect while looking for problematic ones.",
    )

    arguments = parser.parse_args()

    capabilities = anydoc_capabilities()

    if not capabilities["available"]:
        print("AnyDoc is not installed. Install it with: uv pip install firecrawl-anydoc")

        return 1

    print(f"Scanning up to {arguments.scan_limit} cached PDFs for layout problems...")

    candidates = load_candidates(
        database_path=arguments.database,
        scan_limit=arguments.scan_limit,
    )

    selected, triggering = select_problematic(
        candidates=candidates,
        limit=arguments.limit,
    )

    print(
        f"{triggering} of {len(candidates)} distinct cached PDFs trip a layout rule; "
        f"testing {len(selected)}."
    )

    documents: list[dict[str, Any]] = []

    for source, _stored, triggers in selected:
        body = Path(source.local_path).read_bytes()

        # The stored parse is the primary reading. It is re-run rather
        # than trusted so the runtime of both halves is comparable.
        started = time.perf_counter()

        primary = parse_document(
            body=body,
            content_type=source.content_type,
            url=source.url,
        )

        primary_seconds = time.perf_counter() - started

        decision = apply_layout_fallback(
            body=body,
            primary=primary,
            document_type=source.document_type,
            fund_name=source.fund_name,
            verify=lambda reading, chosen=source: extracted_field_names(
                document=reading,
                record=_record_for(source=chosen, document=reading),
                fund_name=chosen.fund_name,
                fund_web=chosen.fund_web,
            ),
        )

        chosen = decision.document if decision.document is not None else primary

        primary_extraction = run_extraction(source=source, document=primary)

        final_extraction = (
            primary_extraction
            if chosen is primary
            else run_extraction(source=source, document=chosen)
        )

        candidate_extraction = None

        if decision.candidate is not None and not decision.accepted:
            candidate_extraction = run_extraction(
                source=source,
                document=decision.candidate,
            )

        print(
            f"  {source.source_id:>7} {decision.outcome.value:<14}"
            f" {'/'.join(t.value for t in triggers)[:52]}"
        )

        documents.append(
            {
                "source_id": source.source_id,
                "fund_name": source.fund_name,
                "url": source.url,
                "document_type": source.document_type,
                "stored_parser_name": source.parser_name,
                "page_count": source.page_count,
                "body_bytes": len(body),
                "triggers": [trigger.value for trigger in triggers],
                "decision": decision.to_json_dict(),
                "blocked_by": blocking_reason(
                    document_type=source.document_type,
                    quality=measure_layout(primary, fund_name=source.fund_name),
                ),
                "runtime": {
                    "primary_seconds": round(primary_seconds, 4),
                    "fallback_seconds": round(decision.conversion_seconds, 4),
                    "overhead_ratio": round(
                        decision.conversion_seconds / max(primary_seconds, 1e-9),
                        3,
                    ),
                },
                "pages_attributed": (
                    {
                        "candidate_pages": decision.candidate.page_count,
                        "with_page_number": sum(
                            1 for page in decision.candidate.pages if page.page_number is not None
                        ),
                        "characters_with_page_number": sum(
                            page.character_count
                            for page in decision.candidate.pages
                            if page.page_number is not None
                        ),
                        "characters_total": decision.candidate.character_count,
                    }
                    if decision.candidate is not None
                    else None
                ),
                "extraction": {
                    "primary": primary_extraction,
                    "final": final_extraction,
                    "rejected_candidate": candidate_extraction,
                    "difference": compare_extractions(
                        primary=primary_extraction,
                        final=final_extraction,
                    ),
                    "difference_if_accepted": (
                        compare_extractions(
                            primary=primary_extraction,
                            final=candidate_extraction,
                        )
                        if candidate_extraction is not None
                        else None
                    ),
                },
            }
        )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "database": str(arguments.database),
        "anydoc": capabilities,
        "documents_scanned": len(candidates),
        "documents_triggering": triggering,
        "documents_tested": len(documents),
        "documents": documents,
        "totals": _totals(documents),
    }

    arguments.report.parent.mkdir(parents=True, exist_ok=True)

    arguments.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _print_summary(report)

    print()
    print(f"Report written to {arguments.report}")

    return 0


def _totals(documents: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(document["decision"]["outcome"] for document in documents)

    triggers = Counter(trigger for document in documents for trigger in document["triggers"])

    accepted = [
        document
        for document in documents
        if document["decision"]["outcome"] == FallbackOutcome.ACCEPTED.value
    ]

    primary_seconds = sum(document["runtime"]["primary_seconds"] for document in documents)

    fallback_seconds = sum(document["runtime"]["fallback_seconds"] for document in documents)

    gained = Counter(
        field for document in documents for field in document["extraction"]["difference"]["gained"]
    )

    lost = Counter(
        field for document in documents for field in document["extraction"]["difference"]["lost"]
    )

    changed = sum(len(document["extraction"]["difference"]["changed"]) for document in documents)

    def counted(key: str, stage: str) -> int:
        return sum(
            int((document["extraction"][stage].get("counts") or {}).get(key, 0))
            for document in documents
        )

    return {
        "outcomes": dict(outcomes),
        "triggers": dict(triggers),
        "accepted": len(accepted),
        "by_document_type": _by_document_type(documents),
        "page_retention": _page_retention(documents),
        "wrong_values": _wrong_values(documents),
        "runtime_seconds": {
            "primary": round(primary_seconds, 3),
            "fallback": round(fallback_seconds, 3),
            "overhead_ratio": round(fallback_seconds / max(primary_seconds, 1e-9), 4),
        },
        "fields": {
            "gained": dict(gained),
            "lost": dict(lost),
            "changed": changed,
            "found_before": counted("fields_found", "primary"),
            "found_after": counted("fields_found", "final"),
            "with_page_before": counted("with_page", "primary"),
            "with_page_after": counted("with_page", "final"),
            "review_required_before": counted("review_required", "primary"),
            "review_required_after": counted("review_required", "final"),
            "high_confidence_before": counted("high_confidence", "primary"),
            "high_confidence_after": counted("high_confidence", "final"),
        },
    }


def _by_document_type(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Report what each kind of document got out of the fallback."""

    by_type: dict[str, dict[str, Any]] = {}

    for document in documents:
        name = str(document["document_type"] or "unknown")

        entry = by_type.setdefault(
            name,
            {
                "triggered": 0,
                "blocked": 0,
                "rejected": 0,
                "accepted": 0,
                "labelled_rows_before": 0,
                "labelled_rows_after": 0,
                "fields_gained": 0,
                "fields_lost": 0,
            },
        )

        entry["triggered"] += 1

        outcome = document["decision"]["outcome"]

        if outcome in entry:
            entry[outcome] += 1

        difference = document["extraction"]["difference"]

        entry["fields_gained"] += len(difference["gained"])

        entry["fields_lost"] += len(difference["lost"])

        if outcome != FallbackOutcome.ACCEPTED.value:
            continue

        primary = document["decision"].get("primary") or {}

        candidate = document["decision"].get("candidate") or {}

        entry["labelled_rows_before"] += int(primary.get("labelled_rows", 0))

        entry["labelled_rows_after"] += int(candidate.get("labelled_rows", 0))

    return by_type


def _page_retention(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Report how much of a conversion kept the page it was written on.

    Measured in characters rather than pages, because one unplaced page
    holding a paragraph is not the same loss as one holding a chapter.
    """

    def totals(only_accepted: bool) -> tuple[int, int, int, int]:
        placed_characters = 0

        total_characters = 0

        placed_pages = 0

        total_pages = 0

        for document in documents:
            attributed = document.get("pages_attributed")

            if not attributed:
                continue

            if only_accepted and document["decision"]["outcome"] != FallbackOutcome.ACCEPTED.value:
                continue

            placed_characters += int(attributed["characters_with_page_number"])

            total_characters += int(attributed["characters_total"])

            placed_pages += int(attributed["with_page_number"])

            total_pages += int(attributed["candidate_pages"])

        return (placed_characters, total_characters, placed_pages, total_pages)

    def described(only_accepted: bool) -> dict[str, Any]:
        placed_characters, total_characters, placed_pages, total_pages = totals(only_accepted)

        return {
            "characters_with_page_number": placed_characters,
            "characters_total": total_characters,
            "character_ratio": (
                round(placed_characters / total_characters, 4) if total_characters else None
            ),
            "pages_with_page_number": placed_pages,
            "pages_total": total_pages,
            "page_ratio": round(placed_pages / total_pages, 4) if total_pages else None,
        }

    return {
        "converted": described(False),
        "accepted": described(True),
    }


def _wrong_values(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return every delivered value the fallback actually altered.

    A field that stayed found but changed its value is the regression
    that matters most, because nothing downstream flags it: the output
    still looks complete and is simply wrong.
    """

    altered: list[dict[str, Any]] = []

    for document in documents:
        if document["decision"]["outcome"] != FallbackOutcome.ACCEPTED.value:
            continue

        difference = document["extraction"]["difference"]

        for change in difference["changed"]:
            altered.append(
                {
                    "source_id": document["source_id"],
                    "document_type": document["document_type"],
                    "field": change["field"],
                    "before": change["before"],
                    "after": change["after"],
                }
            )

        for field in difference["lost"]:
            altered.append(
                {
                    "source_id": document["source_id"],
                    "document_type": document["document_type"],
                    "field": field,
                    "before": "found",
                    "after": "lost",
                }
            )

    return altered


# Above this many documents the per-document table is left to the JSON
# report; a hundred rows in a terminal hide the summary rather than
# supporting it.
PER_DOCUMENT_TABLE_LIMIT = 30


def _print_summary(report: dict[str, Any]) -> None:
    totals = report["totals"]

    documents = report["documents"]

    scanned = max(int(report["documents_scanned"]), 1)

    triggering = int(report.get("documents_triggering", 0))

    print()
    print("=" * 92)
    print("ANYDOC SELECTIVE LAYOUT FALLBACK")
    print("=" * 92)
    print(f"AnyDoc version    : {report['anydoc']['version']}")
    print(f"PDFs inspected    : {report['documents_scanned']}")
    print(f"Trip a layout rule: {triggering} ({triggering / scanned:.0%} of the corpus)")
    print(f"Tested            : {report['documents_tested']}")
    print()
    print("TRIGGERS  : " + ", ".join(f"{k}={v}" for k, v in sorted(totals["triggers"].items())))
    print("OUTCOMES  : " + ", ".join(f"{k}={v}" for k, v in sorted(totals["outcomes"].items())))

    if len(documents) <= PER_DOCUMENT_TABLE_LIMIT:
        _print_document_table(documents)

    _print_by_document_type(totals)

    _print_accepted(documents)

    _print_rejections(documents)

    _print_wrong_values(totals)

    _print_totals(totals)


def _print_document_table(documents: list[dict[str, Any]]) -> None:
    print()
    print(
        f"{'source':>8}  {'outcome':<14}{'rows p/a':>12}{'broken p/a':>14}"
        f"{'pages':>8}{'sec p/a':>16}"
    )
    print("-" * 92)

    for document in documents:
        decision = document["decision"]

        primary = decision.get("primary") or {}

        candidate = decision.get("candidate") or {}

        attributed = document.get("pages_attributed") or {}

        pages = (
            f"{attributed.get('with_page_number', 0)}/{attributed.get('candidate_pages', 0)}"
            if attributed
            else "-"
        )

        print(
            f"{document['source_id']:>8}  {decision['outcome']:<14}"
            f"{primary.get('labelled_rows', 0):>5}/{candidate.get('labelled_rows', 0):<6}"
            f"{primary.get('broken_sentence_ratio', 0):>6.2f}/"
            f"{candidate.get('broken_sentence_ratio', 0):<7.2f}"
            f"{pages:>8}"
            f"{document['runtime']['primary_seconds']:>8.2f}/"
            f"{document['runtime']['fallback_seconds']:<7.2f}"
        )

    print("-" * 92)


def _print_by_document_type(totals: dict[str, Any]) -> None:
    print()
    print("WHICH DOCUMENTS BENEFIT")
    print("-" * 92)
    print(
        f"{'document type':<24}{'trig':>6}{'blk':>6}{'rej':>6}{'acc':>6}"
        f"{'rows accepted p/a':>22}{'fields +/-':>14}"
    )

    rows = sorted(
        totals["by_document_type"].items(),
        key=lambda item: (-item[1]["accepted"], -item[1]["triggered"]),
    )

    for name, entry in rows:
        print(
            f"{name:<24}{entry['triggered']:>6}{entry['blocked']:>6}"
            f"{entry['rejected']:>6}{entry['accepted']:>6}"
            f"{entry['labelled_rows_before']:>10}/{entry['labelled_rows_after']:<11}"
            f"{entry['fields_gained']:>6}/{entry['fields_lost']:<7}"
        )


def _print_accepted(documents: list[dict[str, Any]]) -> None:
    accepted = [
        document
        for document in documents
        if document["decision"]["outcome"] == FallbackOutcome.ACCEPTED.value
    ]

    print()
    print(f"ACCEPTED DOCUMENTS ({len(accepted)})")
    print("-" * 92)

    if not accepted:
        print("  none")

        return

    for document in accepted:
        difference = document["extraction"]["difference"]

        attributed = document.get("pages_attributed") or {}

        placed = attributed.get("characters_with_page_number", 0)

        total = max(attributed.get("characters_total", 0), 1)

        print(
            f"  [{document['source_id']}] {str(document['document_type']):<20}"
            f" pages kept {placed / total:.0%}"
            f"  +{difference['gained']} -{difference['lost']}"
            f"  {'; '.join(document['decision']['improvements'])[:56]}"
        )


def _reason_shape(reason: str) -> str:
    """Return the kind of a rejection reason, without its own numbers."""

    for marker in (
        "of the words",
        "another fund",
        "broken sentences",
        "labelled rows",
        "the extraction would lose",
        "dates",
        "percentages",
        "years",
        "identifiers",
        "attribution check",
    ):
        if marker in reason:
            return marker

    return reason


def _print_rejections(documents: list[dict[str, Any]]) -> None:
    reasons: Counter[str] = Counter()

    would_have_gained: Counter[str] = Counter()

    would_have_lost: Counter[str] = Counter()

    for document in documents:
        if document["decision"]["outcome"] != FallbackOutcome.REJECTED.value:
            continue

        for reason in document["decision"]["regressions"] or ["improved nothing measurable"]:
            # The numbers inside a reason differ per document; the shape
            # of the complaint is what says which rule did the work.
            reasons[_reason_shape(reason)] += 1

        difference = document["extraction"].get("difference_if_accepted") or {}

        would_have_gained.update(difference.get("gained") or [])

        would_have_lost.update(difference.get("lost") or [])

    print()
    print("WHY REPLACEMENTS WERE REFUSED")
    print("-" * 92)

    for reason, count in reasons.most_common():
        print(f"  {count:>4}  {reason}")

    if would_have_gained:
        print(f"  refusing them cost : {dict(would_have_gained)}")

    if would_have_lost:
        print(f"  refusing them saved: {dict(would_have_lost)}")


def _print_wrong_values(totals: dict[str, Any]) -> None:
    wrong = totals["wrong_values"]

    print()
    print("WRONG-VALUE REGRESSIONS")
    print("-" * 92)

    if not wrong:
        print("  none: no accepted replacement changed or dropped a delivered value")

        return

    for entry in wrong:
        print(
            f"  [{entry['source_id']}] {entry['document_type']}: {entry['field']}"
            f" {str(entry['before'])[:40]} -> {str(entry['after'])[:40]}"
        )


def _print_totals(totals: dict[str, Any]) -> None:
    fields = totals["fields"]

    retention = totals["page_retention"]

    print()
    print("=" * 92)
    print("EXTRACTION, EVIDENCE AND RUNTIME")
    print("=" * 92)
    print(f"Fields found      : {fields['found_before']} -> {fields['found_after']}")
    print(f"Fields gained     : {fields['gained'] or 'none'}")
    print(f"Fields lost       : {fields['lost'] or 'none'}")
    print(f"Values changed    : {fields['changed']}")
    print(f"With page number  : {fields['with_page_before']} -> {fields['with_page_after']}")
    print(
        f"Review required   : {fields['review_required_before']} -> "
        f"{fields['review_required_after']}"
    )
    print(
        f"High confidence   : {fields['high_confidence_before']} -> "
        f"{fields['high_confidence_after']}"
    )

    converted = retention["converted"]

    accepted = retention["accepted"]

    if converted["character_ratio"] is not None:
        print(
            f"Page numbers kept : {converted['character_ratio']:.1%} of converted characters, "
            f"{converted['pages_with_page_number']}/{converted['pages_total']} pages"
        )

    if accepted["character_ratio"] is not None:
        print(
            f"  in accepted     : {accepted['character_ratio']:.1%} of characters, "
            f"{accepted['pages_with_page_number']}/{accepted['pages_total']} pages"
        )

    print(
        f"Runtime           : primary {totals['runtime_seconds']['primary']:.1f}s, "
        f"fallback {totals['runtime_seconds']['fallback']:.1f}s "
        f"(+{totals['runtime_seconds']['overhead_ratio']:.1%})"
    )


if __name__ == "__main__":
    sys.exit(main())
