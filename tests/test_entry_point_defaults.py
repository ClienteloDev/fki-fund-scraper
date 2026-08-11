"""
The default paths of the entry points that write or read canonical data.

Each of these defaults was found pointing at a stale artefact during a
documentation review: running the script without arguments quietly
operated on an old subset or an old file. The defaults are part of the
contract now, and these tests are what keeps them from drifting back.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from fundscraper.cli import app

CANONICAL_INPUT = Path("data/input/funds.json")

CANONICAL_OUTPUT = Path("data/output/funds.full.json")

EXTENDED_SCHEMA = Path("schemas/extended-output.schema.json")

# The schema generated before the extended fields existed. It must not be
# the target of anything, because regenerating it would look like an
# update while leaving six delivered fields undocumented.
STALE_SCHEMA = Path("docs/funds-output.schema.json")


def default_of(
    *,
    module: str,
    option: str,
) -> Path:
    """Return the default of one argparse option of one script."""

    import importlib

    parser = importlib.import_module(module).build_parser()

    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public reader
        if option in action.option_strings:
            assert isinstance(action.default, Path)

            return action.default

    raise AssertionError(f"{module} has no option {option}")


def test_offline_re_extraction_defaults_to_the_canonical_input() -> None:
    """`funds.audit-rerun.json` is a historical subset, not the dataset."""

    assert (
        default_of(
            module="scripts.reextract_offline",
            option="--input",
        )
        == CANONICAL_INPUT
    )


def test_the_audit_defaults_to_the_canonical_output() -> None:
    assert (
        default_of(
            module="scripts.audit_enriched_output",
            option="--input",
        )
        == CANONICAL_OUTPUT
    )


def test_the_conflict_report_defaults_to_the_canonical_input() -> None:
    assert (
        default_of(
            module="scripts.report_conflicts",
            option="--input",
        )
        == CANONICAL_INPUT
    )


def test_schema_generation_targets_the_extended_schema(
    tmp_path: Path,
) -> None:
    """
    The generated schema has to be the one that carries every field.

    The default is asserted through the command's help text, so that the
    documented path and the real one cannot diverge.
    """

    result = CliRunner().invoke(
        app,
        [
            "generate-schema",
            "--help",
        ],
    )

    assert result.exit_code == 0

    rendered = " ".join(result.stdout.split())

    assert "schemas" in rendered

    assert "extended-output.schema" in rendered

    assert "funds-output.schema" not in rendered

    # The command still writes wherever it is told to.
    target = tmp_path / "generated.schema.json"

    written = CliRunner().invoke(
        app,
        [
            "generate-schema",
            "--output",
            str(target),
        ],
    )

    assert written.exit_code == 0

    assert target.exists()


def test_the_stale_schema_is_not_referenced_by_the_documentation() -> None:
    """
    The old schema file may stay on disk, but nothing may point at it.

    It predates the six extended fields, so a reader who follows a link
    to it would believe the output has ten properties instead of sixteen.
    """

    for document in [Path("README.md"), *sorted(Path("docs").glob("*.md"))]:
        text = document.read_text(encoding="utf-8")

        for line in text.splitlines():
            if STALE_SCHEMA.name not in line:
                continue

            # It may only be mentioned as stale, never offered as a target.
            assert "stale" in line.lower(), f"{document}: {line}"
