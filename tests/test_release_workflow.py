"""Offline regression checks for the Release workflow's metadata and guards."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/release.yml"


def _metadata_code() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text.split(
        "      - name: Validate integration metadata and derive tag\n", 1
    )[1]
    step = step.split("      - name: Validate derived release tag\n", 1)[0]
    source = step.split("          python - <<'PY'\n", 1)[1].split("\n          PY", 1)[
        0
    ]
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
def test_manifest_derives_release_tag(
    tmp_path: Path, version: str, channel: str
) -> None:
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
        tmp_path,
        "0.5.0a5",
        "0.5.0a5",
        EVENT_MODE="published-fallback",
        EVENT_TAG="v0.5.0a4",
        EVENT_PRERELEASE="true",
    )
    assert bad_tag.returncode != 0
    bad_flag, _ = _run(
        tmp_path,
        "0.5.0a5",
        "0.5.0a5",
        EVENT_MODE="published-fallback",
        EVENT_TAG="v0.5.0a5",
        EVENT_PRERELEASE="false",
    )
    assert bad_flag.returncode != 0


def test_release_draft_only_and_immutable_published_assets() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow and "release_ref:" in workflow
    assert "dry_run:" in workflow
    assert "inputs.dry_run != true" in workflow
    assert "refs/heads/main" in workflow
    assert "github.workflow_sha" in workflow
    assert "contents: read" in workflow and "contents: write" in workflow
    assert "draft: true" in workflow
    assert "zip_sha256" in workflow
    assert "scripts/validate_marketplace_release.py" in workflow
    assert "gh release upload" not in workflow
    assert "automatic repair is forbidden" in workflow
    assert "overwrite_files: true" not in workflow
    assert "overwrite_files: false" in workflow


@pytest.mark.parametrize(
    ("total", "init", "api", "climate", "missing", "pytest_exit", "expected_exit"),
    [
        (74.0, 73.0, 75.0, 65.0, False, 0, 0),
        (73.99, 80.0, 80.0, 80.0, False, 0, 1),
        (80.0, 72.99, 80.0, 80.0, False, 0, 1),
        (80.0, 80.0, 74.99, 80.0, False, 0, 1),
        (80.0, 80.0, 80.0, 64.99, False, 0, 1),
        (80.0, 80.0, 80.0, 80.0, True, 0, 1),
        (80.0, 80.0, 80.0, 80.0, False, 2, 2),
    ],
)
def test_release_test_step_enforces_coverage_and_test_failures(
    tmp_path: Path,
    total: float,
    init: float,
    api: float,
    climate: float,
    missing: bool,
    pytest_exit: int,
    expected_exit: int,
) -> None:
    """Execute the release shell and real coverage checker with controlled pytest."""
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text.split("      - name: Run locked test suite\n", 1)[1].split(
        "      - name:", 1
    )[0]
    shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/check_coverage.py", scripts)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""\
            import os
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            assert args[:4] == ["run", "--locked", "--no-sync", "python"]
            args = args[4:]
            if args[:2] == ["-m", "pytest"]:
                if "--cov-report=json:coverage.json" in args:
                    Path("coverage.json").write_bytes(Path("controlled-coverage.json").read_bytes())
                sys.exit(PYTEST_EXIT)
            os.execv(sys.executable, [sys.executable, *args])
            """).replace("PYTEST_EXIT", str(pytest_exit)),
        encoding="utf-8",
    )
    uv.chmod(0o755)
    report = {
        "totals": {"percent_covered": total},
        "files": {
            f"custom_components/airzoneclouddaikin/{module}": {
                "summary": {
                    "percent_covered": percent,
                    "num_statements": 100,
                    "missing_lines": 100 - int(percent),
                }
            }
            for module, percent in (
                ("__init__.py", init),
                ("airzone_api.py", api),
                ("climate.py", climate),
            )
        },
    }
    if missing:
        del report["files"]["custom_components/airzoneclouddaikin/climate.py"]
    (tmp_path / "controlled-coverage.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    result = subprocess.run(
        ["bash", "-c", shell],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    if expected_exit == 1:
        assert "Coverage gate:" in result.stderr
