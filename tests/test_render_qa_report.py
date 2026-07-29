from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SCRIPT_PATH = PROJECT_ROOT / "scripts" / "render_qa_report.py"


def _write_audit(
    path: Path,
    *,
    sample_size: int = 2,
    failed_funds: int = 0,
    completion_rate: float = 80.0,
) -> None:
    audit: dict[str, Any] = {
        "sample_size": sample_size,
        "fully_completed_funds": 1,
        "partial_funds": (
            max(
                sample_size - 1 - failed_funds,
                0,
            )
        ),
        "empty_funds": 0,
        "failed_funds": failed_funds,
        "fields_found": 8,
        "fields_possible": 10,
        "field_completion_rate": completion_rate,
        "scanned_candidates": 0,
        "grounding": {
            "available": True,
            "packets_total": 2,
            "packets_with_context": 1,
            "packets_without_context": 1,
        },
        "field_summaries": [
            {
                "field": "investment_horizon",
                "total": 2,
                "found": 2,
                "not_found": 0,
                "ambiguous": 0,
                "conflicting": 0,
                "error": 0,
                "pending": 0,
                "invalid": 0,
                "completion_rate": 100.0,
            }
        ],
        "domain_summaries": [
            {
                "domain": "example.com",
                "funds": 2,
                "completed_funds": 1,
                "partial_funds": 1,
                "empty_funds": 0,
                "failed_funds": 0,
                "fields_found": 8,
                "fields_possible": 10,
                "completion_rate": 80.0,
                "documents_parsed": 4,
                "failures": 0,
            }
        ],
        "failure_summaries": [],
        "funds": [
            {
                "fund_id": "fund-1",
                "fund_name": "Fund One",
                "domain": "example.com",
                "adapter_name": "",
                "pipeline_status": "completed",
                "fields_found": 5,
                "missing_fields": [],
                "failure_count": 0,
            },
            {
                "fund_id": "fund-2",
                "fund_name": "Fund Two",
                "domain": "example.com",
                "adapter_name": "",
                "pipeline_status": "partial",
                "fields_found": 3,
                "missing_fields": [
                    "target_return",
                    "assets_under_management",
                ],
                "failure_count": 0,
            },
        ],
        "recommendations": [
            {
                "priority": 2,
                "code": "expand_sources",
                "detail": "Expand sources for unresolved fields.",
            }
        ],
    }

    path.write_text(
        json.dumps(
            audit,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_render_qa_report_passes_quality_gates(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"

    output_path = tmp_path / "report.md"

    _write_audit(audit_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--audit",
            str(audit_path),
            "--output",
            str(output_path),
            "--expected-funds",
            "2",
            "--min-completion-rate",
            "70",
            "--max-failed-funds",
            "0",
            "--strict",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0

    report = output_path.read_text(encoding="utf-8")

    assert "Final QA Report" in report
    assert "**PASS:**" in report
    assert "Fund Two" in report


def test_render_qa_report_returns_failure_in_strict_mode(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"

    output_path = tmp_path / "report.md"

    _write_audit(
        audit_path,
        sample_size=1,
        failed_funds=1,
        completion_rate=20.0,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--audit",
            str(audit_path),
            "--output",
            str(output_path),
            "--expected-funds",
            "2",
            "--min-completion-rate",
            "70",
            "--max-failed-funds",
            "0",
            "--strict",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert output_path.exists()

    report = output_path.read_text(encoding="utf-8")

    assert "**FAIL:**" in report
