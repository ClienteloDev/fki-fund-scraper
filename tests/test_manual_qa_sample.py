"""
What the manual QA sample promises a reviewer.

The sample decides where human attention goes, so its defects are quiet
ones: a field silently missing, a quote invented for a value that never
had one, or a different sample on every run so two reviewers cannot talk
about the same row. Each of those is checked here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "build_manual_qa_sample.py"

_specification = importlib.util.spec_from_file_location(
    "build_manual_qa_sample",
    MODULE_PATH,
)

assert _specification is not None
assert _specification.loader is not None

qa = importlib.util.module_from_spec(_specification)

# Registered before execution because ``dataclass`` resolves annotations
# through ``sys.modules[cls.__module__]``, which a module loaded by spec
# alone is not part of.
sys.modules[_specification.name] = qa

_specification.loader.exec_module(qa)


def internal_fund(
    index: int,
    *,
    document_type: str = "statute",
    confidence: str = "high",
    review_required: bool = False,
    quote: str | None = "Minimální investice činí 1 000 000 Kč.",
) -> dict[str, Any]:
    """One internal record carrying every delivered field."""

    source: dict[str, Any] | None = None

    if quote is not None:
        source = {
            "source": {
                "url": f"https://www.fund{index}.cz/statut.pdf",
                "document_type": document_type,
                "retrieved_at": "2026-01-15T10:00:00Z",
                "title": f"Statut fondu {index}",
            },
            "quote": quote,
            "page": 4,
            "section": None,
        }

    def result() -> dict[str, Any]:
        return {
            "status": "found",
            "value": {"amount": 1_000_000.0},
            "source": source,
            "scope": {"type": "fund"},
            "extraction": {
                "method": "regex",
                "confidence": confidence,
                "review_required": review_required,
            },
        }

    record: dict[str, Any] = {
        "fund_id": f"fund_{index:016x}",
        "name": f"FUND {index} SICAV, a.s.",
        "web": f"https://www.fund{index}.cz/",
    }

    for name in qa.DELIVERY_FIELDS:
        record[name] = result()

    return record


def delivered_fund(
    record: dict[str, Any],
) -> dict[str, Any]:
    delivered: dict[str, Any] = {
        "name": record["name"],
        "web": record["web"],
    }

    for name in qa.DELIVERY_FIELDS:
        delivered[name] = {
            "status": "found",
            "value": record[name]["value"],
        }

    return delivered


def population(
    count: int = 40,
) -> Any:
    internal = [
        internal_fund(
            index,
            document_type=("marketing_page" if index % 3 else "statute"),
            confidence=("low" if index % 4 == 0 else "high"),
            review_required=(index % 5 == 0),
        )
        for index in range(count)
    ]

    return qa.build_rows(
        delivered=[delivered_fund(record) for record in internal],
        internal=internal,
        audit_findings=[],
    )


def test_every_delivered_field_is_reachable_by_the_sample() -> None:
    """
    A field absent from the groups would never be reviewed by anyone.

    The module asserts this at import time; this states it as a test so
    the reason is written down where a reader looks for it.
    """

    grouped = [name for group in qa.FIELD_GROUPS for name in group]

    assert sorted(grouped) == sorted(qa.DELIVERY_FIELDS)
    assert len(grouped) == len(set(grouped))


def test_the_same_inputs_and_seed_give_the_same_sample() -> None:
    """Two reviewers have to be able to talk about row 47."""

    rows = population()

    first = qa.select(rows=rows, size=30, seed=7)

    second = qa.select(rows=population(), size=30, seed=7)

    assert [(row.fund_id, row.field) for row in first] == [
        (row.fund_id, row.field) for row in second
    ]


def test_a_different_seed_gives_a_different_sample() -> None:
    rows = population()

    first = qa.select(rows=rows, size=30, seed=7)

    other = qa.select(rows=rows, size=30, seed=99)

    assert [(row.fund_id, row.field) for row in first] != [
        (row.fund_id, row.field) for row in other
    ]


def test_the_sample_covers_every_field_group() -> None:
    rows = qa.select(rows=population(), size=55, seed=7)

    assert len(rows) == 55

    sampled = {row.field for row in rows}

    for group in qa.FIELD_GROUPS:
        assert sampled & set(group), group


def test_no_fund_dominates_the_sample() -> None:
    counts = Counter(row.fund_id for row in qa.select(rows=population(), size=55, seed=7))

    assert max(counts.values()) <= qa.MAXIMUM_ROWS_PER_FUND


def test_the_sample_prefers_the_values_that_are_hard_to_trust() -> None:
    """
    An easy sample proves nothing.

    Every fund here carries the same eleven values, so the only thing
    separating rows is the evidence behind them.
    """

    rows = population()

    sample = qa.select(rows=rows, size=44, seed=7)

    def flagged(items: list[Any]) -> float:
        return sum(1 for row in items if row.review_required) / len(items)

    assert flagged(sample) > flagged(rows)


def test_a_value_with_no_stored_quote_is_marked_and_not_invented() -> None:
    """
    The rule that matters most: never write evidence that does not exist.

    A delivered value whose source block was lost still belongs in the
    sample — a reviewer should see it — but the row has to say the quote
    is missing rather than leave a blank that reads as "no problem".
    """

    record = internal_fund(1, quote=None)

    rows = qa.build_rows(
        delivered=[delivered_fund(record)],
        internal=[record],
        audit_findings=[],
    )

    assert rows

    for row in rows:
        assert row.evidence_status == "no_source_stored"
        assert row.evidence_quote is None
        assert row.source_url is None

        # Missing evidence makes a row harder, so it is sampled sooner.
        assert row.difficulty >= 4


def test_the_verdict_columns_are_left_for_the_reviewer() -> None:
    for row in qa.select(rows=population(), size=22, seed=7):
        assert row.manual_verdict == ""
        assert row.manual_note == ""


def test_the_csv_keeps_a_multi_line_quote_inside_one_cell(
    tmp_path: Path,
) -> None:
    """A Czech quote spans lines; a broken cell would shift every column."""

    import csv

    record = internal_fund(
        1,
        quote="Fondový kapitál\nSpolečnosti dosáhl k 31. 12. 2022 hodnoty 52 012 tis. Kč.",
    )

    rows = qa.build_rows(
        delivered=[delivered_fund(record)],
        internal=[record],
        audit_findings=[],
    )

    path = tmp_path / "sample.csv"

    qa.write_csv(rows=rows, path=path)

    with path.open(encoding="utf-8-sig", newline="") as handle:
        written = list(csv.DictReader(handle))

    assert len(written) == len(rows)

    assert all("\n" in row["evidence_quote"] for row in written)

    assert all(row["manual_verdict"] == "" for row in written)


def test_the_json_carries_what_the_sample_was_drawn_from(
    tmp_path: Path,
) -> None:
    """A sample nobody can trace back to an input is not evidence of anything."""

    rows = qa.select(rows=population(), size=11, seed=7)

    path = tmp_path / "sample.json"

    qa.write_json(
        rows=rows,
        path=path,
        provenance={
            "input_sha256": "a" * 64,
            "ruleset_version": "2026-08-12.2",
        },
    )

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["input_sha256"] == "a" * 64
    assert payload["ruleset_version"] == "2026-08-12.2"
    assert payload["sample_size"] == 11
    assert len(payload["rows"]) == 11
