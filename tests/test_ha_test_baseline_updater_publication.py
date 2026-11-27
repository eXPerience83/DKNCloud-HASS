"""Regression tests for Home Assistant baseline PR publication."""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ha-test-baseline-updater.yml"


def _sanitize_ref_function() -> str:
    """Extract the real sanitize_ref shell function from the workflow."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^\s{10}sanitize_ref\(\) \{\n.*?^\s{10}\}\n",
        workflow,
    )
    assert match, "sanitize_ref function is missing from baseline updater workflow"
    return textwrap.dedent(match.group(0))


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("2026.9.3", "2026.9.3"),
        ("0.13.366", "0.13.366"),
        ("bad ref/@#", "bad-ref---"),
    ),
)
def test_baseline_pr_ref_sanitizer_is_portable(value: str, expected: str) -> None:
    """Execute the workflow sanitizer and reject the historical tr range bug."""
    if shutil.which("bash") is None or shutil.which("tr") is None:
        pytest.skip("bash and tr are required to execute the publication sanitizer")

    sanitizer = _sanitize_ref_function()
    assert "LC_ALL=C" in sanitizer
    assert "[:alnum:]_.-" in sanitizer
    assert "[:alnum:]._- " not in sanitizer

    # Explicit benign subprocess environment: do not inherit user secrets.
    env = {"VALUE": value, "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail\n{sanitizer}printf '%s' \"$VALUE\" | sanitize_ref\n",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_pytest_summary_replacement_preserves_literal_waiver_reason() -> None:
    """The user reason may contain the placeholder without being rewritten."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    line = next(line.strip() for line in workflow.splitlines() if "jq -Rrs" in line)
    command = line.removesuffix("\\").rstrip()
    original = (
        "- Full pytest and statement coverage: `__PYTEST_RESULT__`\n"
        "<pre>review __PYTEST_RESULT__ literally</pre>\n"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        input=original,
        env={"PATH": "/usr/bin:/bin", "PYTEST_RESULT": "451 passed"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "- Full pytest and statement coverage: `451 passed`" in result.stdout
    assert "<pre>review __PYTEST_RESULT__ literally</pre>" in result.stdout


def test_publication_records_validated_commit_identities() -> None:
    """Bind the PR evidence to the validated base and proposed head."""
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    publication = workflow.split("      - name: Publish validated pull request", 1)[1]
    assert 'test "$(git rev-parse HEAD)" = "$BASE_SHA"' in publication
    assert 'head_sha="$(git rev-parse HEAD)"' in publication
    assert "Validated base commit:" in publication
    assert "Proposed head commit:" in publication
    assert 'if [[ "$remote_status" != 2 ]]; then' in publication
    assert 'exit "$remote_status"' in publication
    assert publication.count('test "$remote_main" = "$BASE_SHA"') == 2
    final_check = publication.rindex('test "$remote_main" = "$BASE_SHA"')
    assert final_check < publication.index("git push")


def test_publication_push_only_creates_absent_branch(tmp_path: Path) -> None:
    """An unexpected branch is never overwritten, even by a fast-forward."""
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git and bash are required to exercise publication lease")

    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    push = next(
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("git push ")
    )
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C"}
    subprocess.run(["git", "init", "--bare", str(remote)], env=env, check=True)
    subprocess.run(["git", "init", str(checkout)], env=env, check=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=checkout,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("config", "user.name", "Baseline test")
    git("config", "user.email", "baseline@example.invalid")
    git("remote", "add", "origin", str(remote))
    git("commit", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    branch = "automation/ha-test-baseline"
    push_env = {**env, "branch": branch}
    subprocess.run(["bash", "-c", push], cwd=checkout, env=push_env, check=True)
    git("commit", "--allow-empty", "-m", "concurrent candidate")
    result = subprocess.run(
        ["bash", "-c", push],
        cwd=checkout,
        env=push_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert git("ls-remote", "origin", f"refs/heads/{branch}").split()[0] == first
