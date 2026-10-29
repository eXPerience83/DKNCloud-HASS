"""Safety and scope invariants of the reviewed HA baseline updater workflow."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "ha-test-baseline-updater.yml"
)


def test_updater_is_schedule_or_manual_only() -> None:
    """Never make the updater a required PR/push check."""
    text = WORKFLOW.read_text(encoding="utf-8")
    header = text.split("permissions:", 1)[0]
    assert "  schedule:" in header
    assert "  workflow_dispatch:" in header
    assert "\n  push:" not in header
    assert "\n  pull_request:" not in header
    assert '        default: false' in header


def test_validation_is_read_only_and_publication_is_gated() -> None:
    """Only the gated publication job can write to GitHub."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in text
    assert "  validation:" in text
    assert "      contents: read" in text
    publication = text.split("\n  publication:\n", 1)[1]
    assert "      contents: write" in publication
    assert "      pull-requests: write" in publication
    assert "github.ref == 'refs/heads/main'" in publication
    assert "needs.validation.outputs.status == 'update'" in publication
    assert "github.event_name == 'schedule'" in publication


def test_artifact_and_base_checks_prevent_unvalidated_publication() -> None:
    """Artifact hashes, main lease, unique ref and approved files are checked."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for requirement in (
        "sha256sum --check SHA256SUMS",
        'test "$remote_main" = "$BASE_SHA"',
        "expected_changes=$'pyproject.toml\\nuv.lock'",
        "git ls-remote --exit-code origin",
        "gh pr list",
        "uv lock --check",
        "validate-lock",
        "uv run --locked --no-sync python -m pytest",
        "uv run --locked --no-sync ruff check .",
        "uv run --locked --no-sync black --check .",
        "artifacts/download",
    ):
        if requirement == "artifacts/download":
            assert "actions/download-artifact@" in text
        else:
            assert requirement in text


def test_scope_contains_only_two_dependency_files() -> None:
    """The updater never edits HA floor, runtime integration or HACS metadata."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "git add pyproject.toml uv.lock" in text
    assert "git add ." not in text
    assert "hacs.json" not in text
    assert "custom_components/" not in text
    assert "gh pr create" in text
    assert "gh pr merge" not in text
