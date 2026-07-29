from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from fundscraper.input_loader import load_funds
from fundscraper.models import FundInput
from fundscraper.output_models import FieldStatus, FundOutput, ProcessingStatus
from fundscraper.output_service import load_output, stable_fund_id


FIELD_NAMES = (
    "investment_horizon",
    "minimum_investment",
    "target_return",
    "fees",
    "assets_under_management",
)


def run_checked(
    label: str,
    command: Sequence[str],
) -> None:
    print()
    print("=" * 72)
    print(label)
    print("=" * 72)
    print(" ".join(command))
    print()

    completed = subprocess.run(
        list(command),
        check=False,
    )

    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.")


def found_field_count(
    output: FundOutput,
) -> int:
    return sum(
        1 for field_name in FIELD_NAMES if getattr(output, field_name).status is FieldStatus.FOUND
    )


def select_weak_funds(
    *,
    funds: list[FundInput],
    outputs: list[FundOutput],
    maximum_fields_found: int,
) -> list[FundInput]:
    outputs_by_id = {output.fund_id: output for output in outputs}

    selected: list[FundInput] = []

    for fund in funds:
        output = outputs_by_id.get(stable_fund_id(fund))

        if output is None:
            selected.append(fund)
            continue

        if output.processing.status is ProcessingStatus.FAILED:
            selected.append(fund)
            continue

        if found_field_count(output) <= maximum_fields_found:
            selected.append(fund)

    return selected


def write_fund_input(
    path: Path,
    funds: list[FundInput],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = [
        fund.model_dump(
            mode="json",
        )
        for fund in funds
    ]

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(path)


def output_metrics(
    outputs: list[FundOutput],
) -> dict[str, Any]:
    distribution = Counter(found_field_count(output) for output in outputs)

    fields_found = sum(found_count * fund_count for found_count, fund_count in distribution.items())

    fields_possible = len(outputs) * len(FIELD_NAMES)

    completion_rate = (
        round(
            fields_found / fields_possible * 100,
            2,
        )
        if fields_possible
        else 0.0
    )

    failed = sum(1 for output in outputs if output.processing.status is ProcessingStatus.FAILED)

    return {
        "funds": len(outputs),
        "fields_found": fields_found,
        "fields_possible": fields_possible,
        "completion_rate": completion_rate,
        "failed_funds": failed,
        "distribution": {
            str(found_count): distribution.get(
                found_count,
                0,
            )
            for found_count in range(6)
        },
    }


def show_metrics(
    label: str,
    outputs: list[FundOutput],
) -> dict[str, Any]:
    metrics = output_metrics(outputs)

    print()
    print(label)
    print("-" * len(label))
    print(f"Funds: {metrics['funds']}")
    print(f"Fields found: {metrics['fields_found']}/{metrics['fields_possible']}")
    print(f"Completion rate: {metrics['completion_rate']}%")
    print(f"Failed funds: {metrics['failed_funds']}")

    distribution = metrics["distribution"]

    for found_count in range(6):
        print(f"{found_count}/5 fields: {distribution[str(found_count)]}")

    return metrics


def backup_file(
    source: Path,
    destination_directory: Path,
) -> None:
    if not source.exists():
        return

    destination_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.copy2(
        source,
        destination_directory / source.name,
    )


def write_json_atomic(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    temporary_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    temporary_path.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the MVP funds pipeline in three stages: "
            "official website, AVANT fallback, "
            "then PorovnejFondy fallback."
        )
    )

    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/input/funds.json"),
    )

    parser.add_argument(
        "--database",
        type=Path,
        default=Path("cache/fundscraper.sqlite3"),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/output/funds.enriched.json"),
    )

    parser.add_argument(
        "--http-cache",
        type=Path,
        default=Path("cache/http"),
    )

    parser.add_argument(
        "--parsed-cache",
        type=Path,
        default=Path("cache/parsed"),
    )

    parser.add_argument(
        "--reports-directory",
        type=Path,
        default=Path("reports"),
    )

    parser.add_argument(
        "--weak-threshold",
        type=int,
        choices=range(0, 5),
        default=2,
        metavar="0-4",
        help=("Funds with this many or fewer found fields continue to the next fallback stage."),
    )

    parser.add_argument(
        "--official-max-pages",
        type=int,
        default=8,
    )

    parser.add_argument(
        "--official-max-depth",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--official-max-documents",
        type=int,
        default=12,
    )

    parser.add_argument(
        "--avant-max-pages",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--avant-max-depth",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--avant-max-documents",
        type=int,
        default=20,
    )

    parser.add_argument(
        "--porovnejfondy-max-pages",
        type=int,
        default=8,
    )

    parser.add_argument(
        "--porovnejfondy-max-depth",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--porovnejfondy-max-documents",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--max-snippets",
        type=int,
        default=8,
    )

    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip Ruff, mypy and pytest before the data run.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=("Ignore HTTP and parsed caches. Normally leave this disabled."),
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    project_root = Path.cwd()

    input_path = args.input.resolve()
    database_path = args.database.resolve()
    output_path = args.output.resolve()
    http_cache = args.http_cache.resolve()
    parsed_cache = args.parsed_cache.resolve()
    reports_directory = args.reports_directory.resolve()

    funds = load_funds(input_path)

    if len(funds) > 230:
        raise ValueError("The current CLI supports at most 230 funds per batch.")

    reports_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    backup_directory = project_root / "backups" / f"before-mvp-final-{timestamp}"

    files_to_backup = (
        output_path,
        database_path,
        reports_directory / "mvp-stage1-official.json",
        reports_directory / "mvp-stage2-avant.json",
        reports_directory / "mvp-stage3-porovnejfondy.json",
        reports_directory / "mvp-final-summary.json",
        reports_directory / "grounding-packets.json",
    )

    for source in files_to_backup:
        backup_file(
            source,
            backup_directory,
        )

    if not args.skip_preflight:
        run_checked(
            "Ruff format check",
            (
                "uv",
                "run",
                "ruff",
                "format",
                "--check",
                ".",
            ),
        )

        run_checked(
            "Ruff lint check",
            (
                "uv",
                "run",
                "ruff",
                "check",
                ".",
            ),
        )

        run_checked(
            "Mypy type check",
            (
                "uv",
                "run",
                "mypy",
            ),
        )

        run_checked(
            "Complete pytest suite",
            (
                "uv",
                "run",
                "pytest",
            ),
        )

    stage1_report = reports_directory / "mvp-stage1-official.json"

    stage2_report = reports_directory / "mvp-stage2-avant.json"

    stage3_report = reports_directory / "mvp-stage3-porovnejfondy.json"

    stage2_input = input_path.parent / "funds.retry.avant.json"

    stage3_input = input_path.parent / "funds.retry.porovnejfondy.json"

    stage2_output = output_path.parent / "funds.retry.avant.enriched.json"

    stage3_output = output_path.parent / "funds.retry.porovnejfondy.enriched.json"

    runtime_files = (
        stage1_report,
        stage2_report,
        stage3_report,
        stage2_input,
        stage3_input,
        stage2_output,
        stage3_output,
    )

    for runtime_file in runtime_files:
        runtime_file.unlink(missing_ok=True)

    run_checked(
        "Reset processing database",
        (
            "uv",
            "run",
            "fundscraper",
            "init-db",
            "--input",
            str(input_path),
            "--database",
            str(database_path),
            "--reset",
        ),
    )

    run_checked(
        "Reset enriched output",
        (
            "uv",
            "run",
            "fundscraper",
            "init-output",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--force",
        ),
    )

    force_arguments = ("--force",) if args.force else ()

    run_checked(
        "Stage 1/3: official websites for all funds",
        (
            "uv",
            "run",
            "fundscraper",
            "run-sample",
            "--input",
            str(input_path),
            "--database",
            str(database_path),
            "--output",
            str(output_path),
            "--report",
            str(stage1_report),
            "--cache-directory",
            str(http_cache),
            "--parsed-directory",
            str(parsed_cache),
            "--limit",
            str(len(funds)),
            "--offset",
            "0",
            "--max-pages",
            str(args.official_max_pages),
            "--max-depth",
            str(args.official_max_depth),
            "--max-documents",
            str(args.official_max_documents),
            "--fresh-output",
            *force_arguments,
        ),
    )

    stage1_outputs = load_output(output_path)

    stage1_metrics = show_metrics(
        "STAGE 1 RESULT – OFFICIAL WEBSITES",
        stage1_outputs,
    )

    avant_funds = select_weak_funds(
        funds=funds,
        outputs=stage1_outputs,
        maximum_fields_found=args.weak_threshold,
    )

    write_fund_input(
        stage2_input,
        avant_funds,
    )

    print()
    print(f"AVANT fallback candidates: {len(avant_funds)}")
    print(
        "AVANT fallback is domain-independent: "
        "every selected fund is checked against AVANT "
        "regardless of its official web domain."
    )

    if avant_funds:
        run_checked(
            "Stage 2/3: AVANT fallback for weak funds",
            (
                "uv",
                "run",
                "fundscraper",
                "run-retry",
                "--input",
                str(stage2_input),
                "--master-input",
                str(input_path),
                "--database",
                str(database_path),
                "--output",
                str(output_path),
                "--retry-output",
                str(stage2_output),
                "--report",
                str(stage2_report),
                "--cache-directory",
                str(http_cache),
                "--parsed-directory",
                str(parsed_cache),
                "--limit",
                "0",
                "--offset",
                "0",
                "--max-pages",
                str(args.avant_max_pages),
                "--max-depth",
                str(args.avant_max_depth),
                "--max-documents",
                str(args.avant_max_documents),
                "--minimum-found-improvement",
                "1",
                "--avant-fallback",
                "--no-porovnejfondy-fallback",
                *force_arguments,
            ),
        )

    stage2_outputs = load_output(output_path)

    stage2_metrics = show_metrics(
        "STAGE 2 RESULT – AFTER AVANT",
        stage2_outputs,
    )

    porovnejfondy_funds = select_weak_funds(
        funds=funds,
        outputs=stage2_outputs,
        maximum_fields_found=args.weak_threshold,
    )

    write_fund_input(
        stage3_input,
        porovnejfondy_funds,
    )

    print()
    print(f"PorovnejFondy fallback candidates: {len(porovnejfondy_funds)}")

    if porovnejfondy_funds:
        run_checked(
            "Stage 3/3: PorovnejFondy fallback for remaining weak funds",
            (
                "uv",
                "run",
                "fundscraper",
                "run-retry",
                "--input",
                str(stage3_input),
                "--master-input",
                str(input_path),
                "--database",
                str(database_path),
                "--output",
                str(output_path),
                "--retry-output",
                str(stage3_output),
                "--report",
                str(stage3_report),
                "--cache-directory",
                str(http_cache),
                "--parsed-directory",
                str(parsed_cache),
                "--limit",
                "0",
                "--offset",
                "0",
                "--max-pages",
                str(args.porovnejfondy_max_pages),
                "--max-depth",
                str(args.porovnejfondy_max_depth),
                "--max-documents",
                str(args.porovnejfondy_max_documents),
                "--minimum-found-improvement",
                "1",
                "--no-avant-fallback",
                "--porovnejfondy-fallback",
                *force_arguments,
            ),
        )

    final_outputs = load_output(output_path)

    if len(final_outputs) != len(funds):
        raise RuntimeError(f"Final output count mismatch: {len(final_outputs)} != {len(funds)}")

    final_metrics = show_metrics(
        "FINAL MVP RESULT",
        final_outputs,
    )

    run_checked(
        "Validate final output",
        (
            "uv",
            "run",
            "fundscraper",
            "validate-output",
            str(output_path),
        ),
    )

    grounding_report = reports_directory / "grounding-packets.json"

    run_checked(
        "Build grounding packets",
        (
            "uv",
            "run",
            "fundscraper",
            "build-grounding-packets",
            "--input",
            str(input_path),
            "--output-data",
            str(output_path),
            "--database",
            str(database_path),
            "--report",
            str(grounding_report),
            "--limit",
            str(len(funds)),
            "--offset",
            "0",
            "--max-snippets",
            str(args.max_snippets),
        ),
    )

    summary_path = reports_directory / "mvp-final-summary.json"

    summary_payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "input_path": str(input_path),
        "output_path": str(output_path),
        "database_path": str(database_path),
        "backup_directory": str(backup_directory),
        "weak_threshold": args.weak_threshold,
        "stages": {
            "official": {
                "report": str(stage1_report),
                "metrics": stage1_metrics,
            },
            "avant": {
                "input": str(stage2_input),
                "report": str(stage2_report),
                "candidates": len(avant_funds),
                "metrics": stage2_metrics,
                "domain_policy": (
                    "AVANT fallback was attempted for every "
                    "weak fund regardless of official domain."
                ),
            },
            "porovnejfondy": {
                "input": str(stage3_input),
                "report": str(stage3_report),
                "candidates": len(porovnejfondy_funds),
                "metrics": final_metrics,
            },
        },
        "final": final_metrics,
        "grounding_report": str(grounding_report),
    }

    write_json_atomic(
        summary_path,
        summary_payload,
    )

    print()
    print("=" * 72)
    print("MVP PIPELINE COMPLETED")
    print("=" * 72)
    print(f"Input funds: {len(funds)}")
    print(f"Final output: {output_path}")
    print(f"Final summary: {summary_path}")
    print(f"Grounding packets: {grounding_report}")
    print(f"Backup: {backup_directory}")
    print()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            "\nRun interrupted by user.",
            file=sys.stderr,
        )
        raise SystemExit(130)
    except Exception as exc:
        print(
            f"\nMVP pipeline failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
