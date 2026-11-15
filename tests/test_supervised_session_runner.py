"""Independent HA acceptance for the same supervised session entry point."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("mode", "field", "spontaneous"),
    [
        ("synthetic-put", "scenary", True),
        ("synthetic-put-after-info", "scenary", False),
        ("synthetic-put", "min_temp_unoccupied", True),
        ("synthetic-put-off", "scenary", True),
    ],
)
def test_isolated_ha_put_and_restore_with_read_window(
    tmp_path, mode, field, spontaneous
):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_supervised_ha.py",
            "--mode",
            mode,
            "--field",
            field,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=320,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["real"] is False
    (tmp_path / "result.json").write_text(json.dumps(report, indent=2))
    assert report["session_passed"] and report["put_without_lab_reads"]
    assert report["put_field"] == field
    assert report["put_push_during_isolation"] is spontaneous
    assert report["put_push_after_resume"] is not spontaneous
    assert report["put_push_received"]
    assert report["backend_control_writes"] == 2
    assert report["restoration_snapshot_confirmed"]
    assert report["pending_http_tasks"] == 0
    assert report["session_closed"] and report["temporary_configuration_removed"]
    assert report["storage_files"] == 0 and report["integration_byte_identical"]
    events = report["trial_timeline"]
    first = next(i for i, e in enumerate(events) if e["kind"] == "window_started")
    end = next(i for i, e in enumerate(events) if e["kind"] == "window_ended")
    assert not any(
        e["kind"] in {"get_admitted", "info_admitted"} for e in events[first:end]
    )
    assert "fake" not in result.stdout and "synthetic-password" not in result.stdout


@pytest.mark.parametrize(
    ("experiment", "mode", "expected_probe"),
    [
        ("info-first", "synthetic-put-after-info", None),
        ("get-first", "synthetic-put-after-info", 19),
        ("get-first", "synthetic-put-stale-get", 18),
        ("spontaneous", "synthetic-put", None),
    ],
)
def test_independent_ha_read_experiments(tmp_path, experiment, mode, expected_probe):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_supervised_ha.py",
            "--mode",
            mode,
            "--field",
            "min_temp_unoccupied",
            "--experiment",
            experiment,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=320,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    (tmp_path / "result.json").write_text(json.dumps(report, indent=2))
    assert report["real"] is False
    assert report["experiment"] == experiment
    assert report["session_passed"] and report["put_without_lab_reads"]
    assert report["info_phase_without_get"]
    assert report["restoration_snapshot_confirmed"]
    assert report["backend_control_writes"] == 2
    assert report["pending_http_tasks"] == 0 and report["session_closed"]
    assert report["temporary_configuration_removed"] and report["storage_files"] == 0
    if expected_probe is not None:
        assert report["get_first_isolated"]
        assert report["get_first_raw_value"] == expected_probe
        assert report["get_first_push"] is False
    assert report["put_push_during_isolation"] is (experiment == "spontaneous")
    assert "synthetic-token" not in result.stdout
