"""Tests for Airzone climate entity behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.core import HomeAssistant

from custom_components.airzoneclouddaikin.climate import AirzoneClimate
from custom_components.airzoneclouddaikin.const import DOMAIN


def _make_climate(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    device_snapshot: dict[str, Any],
    *,
    heat_cool_opt_in: bool,
) -> AirzoneClimate:
    """Instantiate an AirzoneClimate with a lightweight coordinator."""
    entry_id = "entry"
    device_id = "device"
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})[
        "heat_cool_opt_in"
    ] = heat_cool_opt_in

    coordinator = dummy_coordinator_factory({device_id: device_snapshot})
    entity = AirzoneClimate(coordinator, entry_id, device_id)
    entity.hass = hass
    return entity


def test_heat_cool_hidden_without_device_support(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """HEAT_COOL must remain hidden when the device bitmask lacks P2=4."""
    device = {
        "name": "Zone",
        "modes": "11101",
        "mode": "1",
        "power": "1",
    }
    entity = _make_climate(
        hass, dummy_coordinator_factory, device, heat_cool_opt_in=True
    )

    assert entity.hvac_modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]


def test_heat_cool_exposed_when_device_supports_and_opt_in_enabled(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """HEAT_COOL should be exposed when both support and opt-in are true."""
    device = {
        "name": "Zone",
        "modes": "11111",
        "mode": "1",
        "power": "1",
    }
    entity = _make_climate(
        hass, dummy_coordinator_factory, device, heat_cool_opt_in=True
    )

    assert entity.hvac_modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.HEAT_COOL,
        HVACMode.DRY,
    ]


def test_heat_cool_retained_when_current_mode_loses_support(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """The current HEAT_COOL mode should remain visible after support disappears."""
    device = {
        "name": "Zone",
        "modes": "11101",
        "mode": "4",
        "power": "1",
    }
    entity = _make_climate(
        hass, dummy_coordinator_factory, device, heat_cool_opt_in=True
    )

    assert entity.hvac_modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
        HVACMode.HEAT_COOL,
    ]


def test_supported_features_matrix_by_hvac_mode(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Freeze supported_features matrix by HVAC mode, including alias mode 8."""
    base = (
        ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    cases = [
        ({"power": "0", "mode": "1"}, base),
        ({"power": "1", "mode": "5"}, base),
        (
            {"power": "1", "mode": "1"},
            base
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE,
        ),
        (
            {"power": "1", "mode": "2"},
            base
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE,
        ),
        (
            {"power": "1", "mode": "4"},
            base
            | ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE,
        ),
        ({"power": "1", "mode": "3"}, base | ClimateEntityFeature.FAN_MODE),
        ({"power": "1", "mode": "8"}, base | ClimateEntityFeature.FAN_MODE),
    ]

    for snapshot, expected in cases:
        device = {
            "name": "Zone",
            "modes": "11111",
            **snapshot,
        }
        entity = _make_climate(
            hass, dummy_coordinator_factory, device, heat_cool_opt_in=True
        )
        assert entity.supported_features == expected
