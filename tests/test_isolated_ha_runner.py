"""A successful scenario report cannot mask an unsuccessful HA process exit."""

import json
import runpy
import subprocess
from pathlib import Path

import pytest


def test_nonzero_process_exit_fails_even_with_successful_report(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["run_isolated_ha.py"])

    def process(command, **_):
        config = Path(command[-2])
        (config / "lab-result.json").write_text(
            json.dumps(
                {
                    "migrated": True,
                    "push_power": "0",
                    "control_blocked": True,
                    "owned_tasks_closed": True,
                    "backend_control_writes": 0,
                    "platform_entities": {
                        d: 2
                        for d in (
                            "climate",
                            "switch",
                            "number",
                            "sensor",
                            "binary_sensor",
                        )
                    },
                    "blocked_entity_controls": 4,
                    "entity_controls_gated": {str(i): True for i in range(4)},
                    "installations": 2,
                    "fallback_gets": 2,
                    "push_recovered": True,
                    "fallback_spacing": True,
                    "fallback_state_applied": True,
                    "session_closed": True,
                    "server_closed": True,
                    "channel_403_isolated": True,
                    "channel_recovered": True,
                    "healthy_climate_during_403": "off",
                }
            )
        )
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr("subprocess.run", process)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/run_isolated_ha.py")
        )
    assert error.value.code == 1


def test_child_deadline_is_shorter_than_outer_test_and_cleans_configuration(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv", ["run_isolated_ha.py", "--protected", "--process-timeout", "0.1"]
    )

    def expired(command, **kwargs):
        assert kwargs["timeout"] == 0.1
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", expired)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/run_isolated_ha.py")
        )
    assert error.value.code == 1
    assert not list(tmp_path.iterdir())


def test_missing_remote_dev_tmpdir_fails_closed_without_fallback(monkeypatch, capsys):
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr("sys.argv", ["run_isolated_ha.py"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/run_isolated_ha.py")
        )
    assert error.value.code == 2
    assert "Remote Dev TMPDIR is required" in capsys.readouterr().err
