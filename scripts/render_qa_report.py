from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


def load_json_object(
    path: Path,
) -> JsonObject:
    if not path.exists():
        raise FileNotFoundError(f"Required JSON file does not exist: {path}")

    with path.open(
        "r",
        encoding="utf-8-sig",
    ) as handle:
        payload = json.load(handle)

    if not isinstance(
        payload,
        dict,
    ):
        raise ValueError(f"JSON root must be an object: {path}")

    return payload


def as_int(
    value: Any,
) -> int:
    if value is None:
        return 0

    return int(value)


def as_float(
    value: Any,
) -> float:
    if value is None:
        return 0.0

    return float(value)


def as_list(
    value: Any,
) -> list[Any]:
    if isinstance(
        value,
        list,
    ):
        return value

    return []


def markdown_value(
    value: Any,
) -> str:
    return (
        str(value)
        .replace(
            "|",
            r"\|",
        )
        .replace(
            "\r",
            " ",
        )
        .replace(
            "\n",
            " ",
        )
        .strip()
    )


def qa_status(
    passed: bool,
) -> str:
    return "PASS" if passed else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Create a Markdown QA report from a fundscraper audit JSON file.")
    )

    parser.add_argument(
        "--audit",
        type=Path,
        default=Path("reports/full-audit.json"),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/FINAL_QA_REPORT.md"),
    )

    parser.add_argument(
        "--expected-funds",
        type=int,
        default=230,
    )

    parser.add_argument(
        "--min-completion-rate",
        type=float,
        default=70.0,
    )

    parser.add_argument(
        "--max-failed-funds",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help=("Return exit code 1 when at least one quality gate fails."),
    )

    args = parser.parse_args()

    audit = load_json_object(args.audit)

    sample_size = as_int(audit.get("sample_size"))

    fully_completed = as_int(audit.get("fully_completed_funds"))

    partial = as_int(audit.get("partial_funds"))

    empty = as_int(audit.get("empty_funds"))

    failed = as_int(audit.get("failed_funds"))

    fields_found = as_int(audit.get("fields_found"))

    fields_possible = as_int(audit.get("fields_possible"))

    completion_rate = as_float(audit.get("field_completion_rate"))

    scanned_candidates = as_int(audit.get("scanned_candidates"))

    field_summaries = as_list(audit.get("field_summaries"))

    domain_summaries = as_list(audit.get("domain_summaries"))

    failure_summaries = as_list(audit.get("failure_summaries"))

    fund_rows = as_list(audit.get("funds"))

    recommendations = as_list(audit.get("recommendations"))

    grounding_value = audit.get("grounding")

    grounding: JsonObject = (
        grounding_value
        if isinstance(
            grounding_value,
            dict,
        )
        else {}
    )

    invalid_fields = sum(
        as_int(row.get("invalid"))
        for row in field_summaries
        if isinstance(
            row,
            dict,
        )
    )

    fund_ids = [
        str(row.get("fund_id"))
        for row in fund_rows
        if (
            isinstance(
                row,
                dict,
            )
            and row.get("fund_id")
        )
    ]

    duplicate_fund_ids = len(fund_ids) - len(set(fund_ids))

    processed_funds = fully_completed + partial + failed

    quality_gates: list[JsonObject] = [
        {
            "name": "Expected fund count",
            "passed": (sample_size == args.expected_funds),
            "actual": sample_size,
            "required": (args.expected_funds),
        },
        {
            "name": "Processed result count",
            "passed": (processed_funds == args.expected_funds),
            "actual": processed_funds,
            "required": (args.expected_funds),
        },
        {
            "name": "Maximum failed funds",
            "passed": (failed <= args.max_failed_funds),
            "actual": failed,
            "required": (f"<= {args.max_failed_funds}"),
        },
        {
            "name": "Minimum field completion",
            "passed": (completion_rate >= args.min_completion_rate),
            "actual": (f"{completion_rate:.2f}%"),
            "required": (f">= {args.min_completion_rate:.2f}%"),
        },
        {
            "name": "No empty funds",
            "passed": (empty == 0),
            "actual": empty,
            "required": 0,
        },
        {
            "name": "No invalid field structures",
            "passed": (invalid_fields == 0),
            "actual": invalid_fields,
            "required": 0,
        },
        {
            "name": "Unique fund IDs",
            "passed": (duplicate_fund_ids == 0),
            "actual": duplicate_fund_ids,
            "required": 0,
        },
    ]

    all_gates_passed = all(bool(gate.get("passed")) for gate in quality_gates)

    valid_domain_rows = [
        row
        for row in domain_summaries
        if isinstance(
            row,
            dict,
        )
    ]

    lowest_domains = sorted(
        valid_domain_rows,
        key=lambda row: (
            as_float(row.get("completion_rate")),
            str(
                row.get(
                    "domain",
                    "",
                )
            ),
        ),
    )[:25]

    review_funds = sorted(
        (
            row
            for row in fund_rows
            if (
                isinstance(
                    row,
                    dict,
                )
                and (
                    as_int(row.get("fields_found")) < 5
                    or str(
                        row.get(
                            "pipeline_status",
                            "",
                        )
                    )
                    == "failed"
                )
            )
        ),
        key=lambda row: (
            as_int(row.get("fields_found")),
            -as_int(row.get("failure_count")),
            str(
                row.get(
                    "fund_name",
                    "",
                )
            ),
        ),
    )[:60]

    lines: list[str] = []

    lines.extend(
        [
            "# Fundscraper – Final QA Report",
            "",
            (f"Generated from `{args.audit.as_posix()}`."),
            "",
            "## Quality gates",
            "",
            ("| Gate | Result | Actual | Required |"),
            "|---|---:|---:|---:|",
        ]
    )

    for gate in quality_gates:
        lines.append(
            "| "
            f"{markdown_value(gate.get('name'))} | "
            f"{qa_status(bool(gate.get('passed')))} | "
            f"{markdown_value(gate.get('actual'))} | "
            f"{markdown_value(gate.get('required'))} |"
        )

    lines.extend(
        [
            "",
            "## Global summary",
            "",
            "| Metric | Value |",
            "|---|---:|",
            (f"| Funds in audit | {sample_size} |"),
            (f"| Fully completed | {fully_completed} |"),
            (f"| Partial | {partial} |"),
            (f"| Empty | {empty} |"),
            (f"| Pipeline failures | {failed} |"),
            (f"| Fields found | {fields_found}/{fields_possible} |"),
            (f"| Field completion | {completion_rate:.2f}% |"),
            (f"| Scanned document candidates | {scanned_candidates} |"),
            "",
            "## Field results",
            "",
            (
                "| Field | Found | Not found | "
                "Ambiguous | Conflicting | "
                "Error | Pending | Invalid | "
                "Completion |"
            ),
            ("|---|---:|---:|---:|---:|---:|---:|---:|---:|"),
        ]
    )

    for row in field_summaries:
        if not isinstance(
            row,
            dict,
        ):
            continue

        lines.append(
            "| "
            f"{markdown_value(row.get('field', ''))} | "
            f"{as_int(row.get('found'))} | "
            f"{as_int(row.get('not_found'))} | "
            f"{as_int(row.get('ambiguous'))} | "
            f"{as_int(row.get('conflicting'))} | "
            f"{as_int(row.get('error'))} | "
            f"{as_int(row.get('pending'))} | "
            f"{as_int(row.get('invalid'))} | "
            f"{as_float(row.get('completion_rate')):.2f}% |"
        )

    lines.extend(
        [
            "",
            "## Lowest-performing domains",
            "",
            (
                "| Domain | Funds | Completed | "
                "Partial | Empty | Failed | "
                "Fields | Completion | Documents |"
            ),
            ("|---|---:|---:|---:|---:|---:|---:|---:|---:|"),
        ]
    )

    for row in lowest_domains:
        lines.append(
            "| "
            f"{markdown_value(row.get('domain', ''))} | "
            f"{as_int(row.get('funds'))} | "
            f"{as_int(row.get('completed_funds'))} | "
            f"{as_int(row.get('partial_funds'))} | "
            f"{as_int(row.get('empty_funds'))} | "
            f"{as_int(row.get('failed_funds'))} | "
            f"{as_int(row.get('fields_found'))}/"
            f"{as_int(row.get('fields_possible'))} | "
            f"{as_float(row.get('completion_rate')):.2f}% | "
            f"{as_int(row.get('documents_parsed'))} |"
        )

    lines.extend(
        [
            "",
            "## Failure stages",
            "",
        ]
    )

    if failure_summaries:
        lines.extend(
            [
                "| Stage | Count | Examples |",
                "|---|---:|---|",
            ]
        )

        for row in failure_summaries:
            if not isinstance(
                row,
                dict,
            ):
                continue

            examples_value = row.get("examples")

            examples = (
                examples_value
                if isinstance(
                    examples_value,
                    list,
                )
                else []
            )

            example_text = "; ".join(str(example) for example in examples)

            lines.append(
                "| "
                f"{markdown_value(row.get('stage', ''))} | "
                f"{as_int(row.get('count'))} | "
                f"{markdown_value(example_text)} |"
            )
    else:
        lines.append("No pipeline failures were recorded.")

    lines.extend(
        [
            "",
            "## Funds requiring review",
            "",
            ("| Fund | Domain | Adapter | Status | Found | Missing fields | Failures |"),
            "|---|---|---|---|---:|---|---:|",
        ]
    )

    for row in review_funds:
        missing_value = row.get("missing_fields")

        missing_fields = (
            missing_value
            if isinstance(
                missing_value,
                list,
            )
            else []
        )

        missing_text = ", ".join(str(field) for field in missing_fields)

        lines.append(
            "| "
            f"{markdown_value(row.get('fund_name', ''))} | "
            f"{markdown_value(row.get('domain', ''))} | "
            f"{markdown_value(row.get('adapter_name', ''))} | "
            f"{markdown_value(row.get('pipeline_status', ''))} | "
            f"{as_int(row.get('fields_found'))}/5 | "
            f"{markdown_value(missing_text)} | "
            f"{as_int(row.get('failure_count'))} |"
        )

    lines.extend(
        [
            "",
            "## Grounding packets",
            "",
            "| Metric | Value |",
            "|---|---:|",
            (f"| Available | {bool(grounding.get('available', False))} |"),
            (f"| Packets total | {as_int(grounding.get('packets_total'))} |"),
            (f"| With context | {as_int(grounding.get('packets_with_context'))} |"),
            (f"| Without context | {as_int(grounding.get('packets_without_context'))} |"),
            "",
            "## Recommended next actions",
            "",
        ]
    )

    valid_recommendations = [
        item
        for item in recommendations
        if isinstance(
            item,
            dict,
        )
    ]

    if valid_recommendations:
        for item in sorted(
            valid_recommendations,
            key=lambda value: as_int(value.get("priority")),
        ):
            lines.append(
                "- "
                f"**P{as_int(item.get('priority'))} – "
                f"{markdown_value(item.get('code', ''))}:** "
                f"{markdown_value(item.get('detail', ''))}"
            )
    else:
        lines.append("- No automatic recommendations were generated.")

    lines.extend(
        [
            "",
            "## Final result",
            "",
        ]
    )

    if all_gates_passed:
        lines.append("**PASS:** All configured quality gates passed.")
    else:
        lines.append(
            "**FAIL:** At least one configured quality gate failed. "
            "Review the affected funds and audit results before "
            "treating the dataset as final."
        )

    lines.append("")

    args.output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    args.output.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    print(f"QA report written: {args.output}")

    for gate in quality_gates:
        print(
            f"{qa_status(bool(gate.get('passed')))}: "
            f"{gate.get('name')} "
            f"(actual={gate.get('actual')}, "
            f"required={gate.get('required')})"
        )

    if args.strict and not all_gates_passed:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
