"""Offline safety and schedule gates for the two DKN Sleep LAB plans."""

import pytest

from scripts.sleep_lab_plan import main, make_plan


def test_off_then_on_schedule_has_no_home_during_observation():
    plan = make_plan("off_sleep_then_on")
    assert plan.required_start_power == 0
    assert plan.observation_seconds >= 600
    assert plan.hard_deadline_seconds > plan.observation_seconds
    assert [(s.at_second, s.action) for s in plan.steps if s.control] == [
        (-60, "configure_duration"),
        (0, "select_sleep"),
        (240, "power_on"),
    ]
    assert all("occupied" not in str(s.payload) for s in plan.steps if s.at_second >= 0)


def test_on_then_off_schedule_does_not_fake_backend_home():
    plan = make_plan("on_sleep_then_off")
    assert plan.required_start_power == 1
    assert plan.observation_seconds >= 390
    assert [(s.at_second, s.action) for s in plan.steps if s.control] == [
        (-60, "configure_duration"),
        (0, "select_sleep"),
        (120, "manual_power_off"),
    ]
    assert all("occupied" not in str(s.payload) for s in plan.steps if s.at_second >= 0)


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError):
        make_plan("another")


def test_live_execution_is_always_disabled(capsys):
    with pytest.raises(SystemExit) as failure:
        main(["--real"])
    assert failure.value.code == 2
    assert "REAL EXECUTION DISABLED" in capsys.readouterr().err


def test_offline_preview_contains_both_plans(capsys):
    assert main(["--scenario", "both"]) == 0
    result = capsys.readouterr().out
    assert '"network_enabled": false' in result
    assert '"off_sleep_then_on"' in result
    assert '"on_sleep_then_off"' in result
