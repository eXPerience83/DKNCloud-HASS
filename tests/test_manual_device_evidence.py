"""Regression tests for offline, allowlisted real-device evidence collection."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts import manual_device_evidence as evidence


@pytest.fixture(autouse=True)
def isolated_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write evidence into a developer's actual local secrets tree."""
    monkeypatch.setattr(evidence, "RUNS", tmp_path / "manual-runs")


def test_record_and_export_only_allowlisted_data() -> None:
    """The ZIP contains exactly validated metadata, observations and summary."""
    name = evidence.start("pr102", "2026.10.4", "a" * 40)
    evidence.record(name, "official_app", "action", "official_app", "pass", 0)
    evidence.record(
        name,
        "refresh_102",
        "snapshot_after",
        "backend_snapshot",
        "pass",
        12.4,
        "on",
        "cool",
        "auto",
        22.5,
    )
    # A forgotten raw log must never be included in the exported ZIP.
    (evidence.RUNS / name / "raw-request.log").write_text(
        "user_token=private-secret", encoding="utf-8"
    )
    path = evidence.finish(name)
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {
            "session.json",
            "observations.jsonl",
            "SUMMARY.md",
        }
        body = "\n".join(archive.read(item).decode() for item in archive.namelist())
    assert '"variant": "pr102"' in body
    assert "refresh_102" in body
    assert "22.5" in body
    assert "token" not in body.lower()
    assert "password" not in body.lower()
    with pytest.raises(ValueError, match="finalized"):
        evidence.record(name, "reload", "verification", "tester", "pass")


def test_rejects_free_text_and_sensitive_identifiers() -> None:
    name = evidence.start("main", "2026.10.4")
    with pytest.raises(ValueError, match="allowlisted"):
        evidence.record(name, "owner@example.com", "action", "tester", "pass")
    with pytest.raises(ValueError, match="allowlisted"):
        evidence.record(name, "setup", "action", "tester", "token-123")
    with pytest.raises(ValueError, match="generated run name"):
        evidence.record("../outside", "setup", "action", "tester", "pass")
    with pytest.raises(ValueError, match="Invalid variant"):
        evidence.start("owner@example.com", "2026.10.4")


def test_export_refuses_tampered_observation() -> None:
    name = evidence.start("main", "2026.10.4")
    observation = evidence.RUNS / name / "observations.jsonl"
    observation.write_text(
        json.dumps({"email": "owner@example.com"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unexpected fields"):
        evidence.finish(name)
    assert not (evidence.RUNS / name / "evidence.zip").exists()


def test_export_refuses_tampered_metadata() -> None:
    name = evidence.start("main", "2026.10.4")
    evidence.record(name, "setup", "verification", "tester", "pass")
    metadata_path = evidence.RUNS / name / "session.json"
    data = json.loads(metadata_path.read_text())
    data["started_utc"] = "owner@example.com"
    metadata_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid session timestamp"):
        evidence.finish(name)


def test_refuses_empty_run_and_invalid_measurements() -> None:
    name = evidence.start("main", "2026.10.4")
    with pytest.raises(ValueError, match="No observations"):
        evidence.finish(name)
    with pytest.raises(ValueError, match="Elapsed seconds"):
        evidence.record(name, "setup", "verification", "tester", "pass", -1)
    with pytest.raises(ValueError, match="Setpoint"):
        evidence.record(
            name,
            "ha_command",
            "ha_observed",
            "home_assistant",
            "pass",
            setpoint_c=88,
        )
