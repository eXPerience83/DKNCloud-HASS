"""Offline-only plan for supervised DKN Cloud Sleep experiments.

There is deliberately no network access, credential loading, or control API.
Real execution requires a separately reviewed LAB runner and an expiring grant.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Literal

ScenarioName = Literal[
    "off_sleep_then_on",
    "on_sleep_then_panel_off",
    "on_sleep_then_off",
    "one_minute_sleep_rewake",
]
REWAKE_SCENARIO = "one_minute_sleep_rewake"
AUTOMATIC_SCENARIOS = ("off_sleep_then_on", "on_sleep_then_off", REWAKE_SCENARIO)
SCENARIOS = (*AUTOMATIC_SCENARIOS, "on_sleep_then_panel_off")


@dataclass(frozen=True, slots=True)
class Step:
    at_second: int
    action: str
    description: str
    control: bool = False
    payload: dict | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    name: ScenarioName
    observation_seconds: int
    hard_deadline_seconds: int
    required_start_power: int
    required_start_scene: str
    required_mode: str
    required_fan_speed: int
    steps: tuple[Step, ...]

    sleep_minutes: int = 5
    wait_for_off_seconds: int = 0

    @property
    def state_budget(self) -> int:
        if self.wait_for_off_seconds:
            # Baseline, initial polling, corroboration, wake, and follow-up.
            return 8 + self.observation_seconds // 10
        return 6 + self.observation_seconds // 30

    @property
    def intervention(self) -> Step:
        return next(step for step in self.steps if step.at_second > 0)

    def as_dict(self) -> dict:
        return asdict(self)


SLEEP_PUT = {"device": {"scenary": "sleep"}}
SLEEP_TIME_PUT = {"sleep_time": 5}
POWER_ON = {
    "event": {
        "cgi": "modmaquina",
        "option": "P1",
        "value": "1",
        "device_id": "<APPROVED_DEVICE>",
    }
}
POWER_OFF = {
    "event": {
        "cgi": "modmaquina",
        "option": "P1",
        "value": "0",
        "device_id": "<APPROVED_DEVICE>",
    }
}


def make_plan(name: ScenarioName) -> Plan:
    """Return an immutable, no-I/O schedule relative to the Sleep acknowledgement."""
    if name not in SCENARIOS:
        raise ValueError("Unknown experiment")

    minutes = 1 if name == REWAKE_SCENARIO else 5
    common = (
        Step(
            -90,
            "preflight",
            "Confirm approved device, no competing HA/app automations, P2=3 and low fan speed 1, with physically observed power and scene.",
        ),
        Step(
            -60,
            "configure_duration",
            f"PUT sleep_time={minutes}, then require exact readback.",
            True,
            {"sleep_time": minutes},
        ),
        Step(
            -15,
            "baseline",
            "Verify fresh GET, physical power, raw occupied scene, FAN_ONLY P2=3 and low fan. If inconsistent, STOP.",
        ),
        Step(
            0,
            "select_sleep",
            "PUT scenary=sleep once; use the HTTP acknowledgement as t0; read back without changing power.",
            True,
            SLEEP_PUT,
        ),
    )
    if name == "off_sleep_then_on":
        plan = Plan(
            name=name,
            observation_seconds=600,
            hard_deadline_seconds=780,
            required_start_power=0,
            required_start_scene="occupied",
            required_mode="3",
            required_fan_speed=1,
            steps=common
            + (
                Step(
                    240,
                    "power_on",
                    "Send P1=1 once; do NOT send occupied. Observe and record acceptance.",
                    True,
                    POWER_ON,
                ),
                Step(
                    300,
                    "observe_t5",
                    "Check for physical OFF around five minutes after Sleep selection.",
                ),
                Step(
                    540,
                    "observe_t9",
                    "Check for physical OFF around five minutes after ON.",
                ),
                Step(
                    600,
                    "end_observation",
                    "Close evidence and classify; supervised restoration is separate from inference.",
                ),
            ),
        )
    elif name == REWAKE_SCENARIO:
        plan = Plan(
            name=name,
            observation_seconds=90,
            hard_deadline_seconds=360,
            required_start_power=1,
            required_start_scene="occupied",
            required_mode="3",
            required_fan_speed=1,
            steps=common
            + (
                Step(
                    20,
                    "power_on",
                    "20 seconds after confirming OFF, send P1 ON once.",
                    True,
                    POWER_ON,
                ),
            ),
            sleep_minutes=minutes,
            wait_for_off_seconds=120,
        )
    else:
        plan = Plan(
            name=name,
            observation_seconds=390,
            hard_deadline_seconds=570,
            required_start_power=1,
            required_start_scene="occupied",
            required_mode="3",
            required_fan_speed=1,
            steps=common
            + (
                Step(120, "panel_off", "Owner turns OFF at the panel and confirms.")
                if name == "on_sleep_then_panel_off"
                else Step(
                    120,
                    "manual_power_off",
                    "Send P1=0 once; do NOT send occupied. Observe raw backend scene.",
                    True,
                    POWER_OFF,
                ),
                Step(
                    150,
                    "observe_off",
                    "Verify physical OFF and raw scene; check for competing writers.",
                ),
                Step(300, "observe_t5", "Check raw scene at nominal Sleep deadline."),
                Step(
                    390,
                    "end_observation",
                    "Close evidence and classify; supervised restoration is separate from inference.",
                ),
            ),
        )
    _validate(plan)
    return plan


def _validate(plan: Plan) -> None:
    times = [s.at_second for s in plan.steps]
    assert times == sorted(times), "Steps must remain ordered"
    assert sum(s.action == "select_sleep" for s in plan.steps) == 1
    assert plan.intervention.action in {"panel_off", "power_on", "manual_power_off"}
    assert plan.hard_deadline_seconds > plan.observation_seconds
    assert plan.required_start_scene == "occupied"
    assert plan.required_fan_speed == 1
    assert all(s.payload is None or s.control for s in plan.steps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline-only DKN Sleep LAB plan. No credentials, network, or device writes."
    )
    parser.add_argument(
        "--scenario",
        choices=[*SCENARIOS, "all", "both", "automatic"],
        default="all",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Always rejected; real execution needs a separately approved guarded runner.",
    )
    args = parser.parse_args(argv)
    if args.real:
        parser.error(
            "REAL EXECUTION DISABLED: source-bound authorization and a separately reviewed LAB transport are required"
        )
    selected = (
        ("off_sleep_then_on", "on_sleep_then_off")
        if args.scenario == "both"
        else AUTOMATIC_SCENARIOS
        if args.scenario == "automatic"
        else SCENARIOS
        if args.scenario == "all"
        else (args.scenario,)
    )
    print(
        json.dumps(
            {
                "mode": "offline_plan_only",
                "network_enabled": False,
                "plans": [make_plan(x).as_dict() for x in selected],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
