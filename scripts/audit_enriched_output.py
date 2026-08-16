"""
Audit the enriched fund output and classify every field.

The script is read-only. It never writes to the audited file. Each field
of each fund is classified as valid, suspicious, conflicting or missing,
and every suspicious or conflicting field is reported with its value, its
source, the reason and a recommended action.

Usage examples:

    uv run python scripts/audit_enriched_output.py
    uv run python scripts/audit_enriched_output.py \\
        --input funds.enriched.json \\
        --report reports/output-audit.json
    uv run python scripts/audit_enriched_output.py --examples 20
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from fundscraper.output_audit import (
    ALL_AUDITED_FIELDS,
    AuditStatus,
    OutputAuditError,
    audit_enriched_output,
    file_digest,
    load_enriched_records,
    write_audit_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_enriched_output",
        description=(
            "Classify every field of the enriched fund output as valid, "
            "suspicious, conflicting or missing. The audited file is only read."
        ),
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/output/funds.full.json"),
        help="Enriched output to audit.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/output-audit.json"),
        help="Path of the generated JSON audit report.",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=10,
        help="Number of representative findings printed to the console.",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    input_path = args.input.resolve()

    try:
        records = load_enriched_records(input_path)

        report = audit_enriched_output(
            records=records,
            input_path=input_path,
            input_sha256=file_digest(input_path),
        )

        write_audit_report(
            report=report,
            path=args.report.resolve(),
        )
    except OutputAuditError as exc:
        print(
            f"Audit failed: {exc}",
            file=sys.stderr,
        )

        return 1

    summary = report.summary

    print(f"Audited file:  {input_path}")
    print(f"SHA-256:       {report.input_sha256}")
    print(f"Funds:         {summary.funds}")
    print(f"Fields:        {summary.fields_checked}")
    print()

    print("STATUS")
    print("-" * 62)

    for status in AuditStatus:
        print(f"  {status.value:<14} {summary.by_status.get(status.value, 0):>6}")

    print()
    print("SEVERITY OF FINDINGS")
    print("-" * 62)

    for severity, count in summary.by_severity.items():
        print(f"  {severity:<48}{count:>6}")

    print()
    print("FIELD")
    print("-" * 62)
    print(f"  {'field':<24}{'valid':>7}{'suspic.':>9}{'confl.':>7}{'reject':>7}{'missing':>9}")

    for field in ALL_AUDITED_FIELDS:
        counts = summary.by_field.get(field, {})

        print(
            f"  {field:<24}"
            f"{counts.get('valid', 0):>7}"
            f"{counts.get('suspicious', 0):>9}"
            f"{counts.get('conflicting', 0):>7}"
            f"{counts.get('rejected', 0):>7}"
            f"{counts.get('missing', 0):>9}"
        )

    if summary.weakest_fields:
        print()
        print("WEAKEST FIELDS, BY SHARE OF DELIVERED VALUES THAT ARE DOUBTED")
        print("-" * 62)

        for entry in summary.weakest_fields[:6]:
            print(f"  {entry}")

    print()
    print("REASON")
    print("-" * 62)

    for reason, count in summary.by_reason.items():
        statuses = summary.by_reason_status.get(reason, {})

        worst = next(
            (
                status
                for status in ("rejected", "conflicting", "suspicious")
                if statuses.get(status)
            ),
            "-",
        )

        print(f"  {reason:<44}{worst:>12}{count:>6}")

    print()
    print("SOURCE TYPE OF FLAGGED VALUES")
    print("-" * 62)

    for source_type, count in summary.by_source_type.items():
        print(f"  {source_type:<48}{count:>6}")

    print()
    print("SCOPE OF FLAGGED VALUES")
    print("-" * 62)

    for scope, count in summary.by_scope.items():
        print(f"  {scope:<48}{count:>6}")

    if report.schema_notes:
        print()
        print("SCHEMA LIMITATIONS")
        print("-" * 62)

        for note in report.schema_notes:
            print(f"  - {note}")

    if args.examples > 0 and report.findings:
        print()
        print("REPRESENTATIVE FINDINGS")
        print("-" * 62)

        seen: Counter[str] = Counter()

        shown = 0

        for finding in report.findings:
            # One example per reason keeps the console output readable.
            if seen[finding.reason_code]:
                continue

            seen[finding.reason_code] += 1

            shown += 1

            # A series field carries hundreds of observations, and
            # printing them would bury the finding they belong to.
            shown_value = str(finding.normalized_value or finding.extracted_value)

            print(f"  [{finding.status.value}] {finding.field} - {finding.reason_code}")
            print(f"      fund:   {finding.fund_name}")
            print(f"      value:  {shown_value[:120]}")
            print(f"      source: {(finding.source_url or '-')[:90]}")

            if finding.evidence:
                print(f"      text:   {finding.evidence[:90]}")

            print(f"      action: {finding.recommended_action}")
            print()

            if shown >= args.examples:
                break

    print(f"Full report: {args.report.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
