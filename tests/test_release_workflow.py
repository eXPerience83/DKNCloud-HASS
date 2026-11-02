"""Offline regression checks for the Release workflow's metadata and guards."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/release.yml"


def _metadata_code() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text.split("      - name: Validate integration metadata and derive tag\n", 1)[1]
    step = step.split("      - name: Validate derived release tag\n", 1)[0]
    source = step.split("          python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(source)


def _run(tmp_path: Path, version: str, changelog: str, **updates: str):
    path = tmp_path / "custom_components/airzoneclouddaikin"
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(
        f'{{"domain": "airzoneclouddaikin", "version": "{version}"}}',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{changelog}] - 2026-11-03\n",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "EVENT_MODE": "prepare",
        "EVENT_TAG": "",
        "EVENT_PRERELEASE": "",
        **updates,
    }
    result = subprocess.run(
        [sys.executable, "-c", _metadata_code()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output


@pytest.mark.parametrize(
    ("version", "channel"),
    [
        ("0.5.0a5", "true"),
        ("0.5.0b1", "true"),
        ("0.6.0rc1", "true"),
        ("0.6.0", "false"),
    ],
)
def test_manifest_derives_release_tag(tmp_path: Path, version: str, channel: str) -> None:
    result, output = _run(tmp_path, version, version)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == [
        f"version={version}",
        f"tag=v{version}",
        f"prerelease={channel}",
    ]


def test_changelog_mismatch_rejected(tmp_path: Path) -> None:
    result, _ = _run(tmp_path, "0.5.0a5", "0.5.0a4")
    assert result.returncode != 0
    assert "CHANGELOG.md" in result.stderr


@pytest.mark.parametrize("version", ["v0.5.0", "0.5.0-dev", "01.5.0", "0.5.0;echo"])
def test_malformed_versions_rejected(tmp_path: Path, version: str) -> None:
    result, _ = _run(tmp_path, version, version)
    assert result.returncode != 0


def test_published_tag_or_prerelease_mismatch_rejected(tmp_path: Path) -> None:
    bad_tag, _ = _run(
        tmp_path, "0.5.0a5", "0.5.0a5",
        EVENT_MODE="published-fallback",
        EVENT_TAG="v0.5.0a4",
        EVENT_PRERELEASE="true",
    )
    assert bad_tag.returncode != 0
    bad_flag, _ = _run(
        tmp_path, "0.5.0a5", "0.5.0a5",
        EVENT_MODE="published-fallback",
        EVENT_TAG="v0.5.0a5",
        EVENT_PRERELEASE="false",
    )
    assert bad_flag.returncode != 0


def test_release_draft_only_and_immutable_published_assets() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow and "release_ref:" in workflow
    assert "refs/heads/main" in workflow
    assert "github.workflow_sha" in workflow
    assert "contents: read" in workflow and "contents: write" in workflow
    assert "draft: true" in workflow
    assert "zip_sha256" in workflow
    assert "scripts/validate_marketplace_release.py" in workflow
    assert "gh release upload" in workflow
    assert "Published release already contains airzoneclouddaikin.zip" in workflow
    assert re.search(r"overwrite_files: true", workflow)  # draft updates only
