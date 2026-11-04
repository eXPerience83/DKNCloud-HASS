"""Regression checks for shared event payload and state helpers."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.airzoneclouddaikin.commands import modmaquina_payload
from custom_components.airzoneclouddaikin.device_state import (
    power_is_on,
    preset_to_scenary,
    scenary_to_preset,
)


def test_modmaquina_payload_preserves_canonical_event_shape_and_value_type() -> None:
    """The common builder must preserve original event nesting and numeric types."""
    assert modmaquina_payload("dev1", "P1", 1) == {
        "event": {
            "cgi": "modmaquina",
            "device_id": "dev1",
            "option": "P1",
            "value": 1,
        }
    }
    assert modmaquina_payload("dev1", "P7", "23.0")["event"]["value"] == "23.0"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (" ON ", True),
        ("YES", True),
        (2, True),
        ("2", True),
        (False, False),
        (0, False),
        ("0", False),
        ("none", False),
        ("", False),
        (None, False),
        ("unknown", False),
    ],
)
def test_power_is_on_preserves_climate_and_switch_interpretation(
    value: Any, expected: bool
) -> None:
    """Both entities normalize the same backend and optimistic power values."""
    assert power_is_on(value) is expected


@pytest.mark.parametrize(
    ("scenary", "preset"),
    [
        ("occupied", "home"),
        (" VACANT ", "away"),
        ("sleep", "sleep"),
    ],
)
def test_known_scenary_preset_roundtrip(scenary: str, preset: str) -> None:
    """Keep the existing bidirectional DKN/HA scene mapping."""
    assert scenary_to_preset(scenary) == preset
    assert preset_to_scenary(preset) == scenary.strip().lower()


def test_unknown_scene_is_not_treated_as_a_known_preset() -> None:
    """Unknown scene telemetry stays distinct from a selectable HA preset."""
    assert scenary_to_preset("custom") is None
    assert preset_to_scenary("custom") is None
