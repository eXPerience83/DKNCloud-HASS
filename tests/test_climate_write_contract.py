"""Deterministic DKN climate write contract tests: no real devices or cloud."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant

import custom_components.airzoneclouddaikin.climate as climate_module
from custom_components.airzoneclouddaikin.climate import AirzoneClimate
from custom_components.airzoneclouddaikin.const import DOMAIN


def _climate(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "1",
    power: str = "1",
    scenary: str = "occupied",
) -> tuple[AirzoneClimate, Any]:
    """Create an independent climate entity backed by a synthetic DKN device."""
    dev = {
        "id": "unit-1",
        "name": "Test Zone",
        "mode": mode,
        "power": power,
        "scenary": scenary,
        "modes": "11111",
        "availables_speeds": "3",
        "cold_speed": "2",
        "heat_speed": "2",
        "local_temp": "21.0",
        "cold_consign": "24.0",
        "heat_consign": "20.0",
        "min_limit_cold": 18,
        "max_limit_cold": 28,
        "min_limit_heat": 16,
        "max_limit_heat": 30,
    }
    api = SimpleNamespace(send_event=AsyncMock(), put_device_fields=AsyncMock())
    hass.data.setdefault(DOMAIN, {}).setdefault("climate-entry", {})[
        "heat_cool_opt_in"
    ] = True
    coordinator = dummy_coordinator_factory({"unit-1": dev}, api=api)
    entity = AirzoneClimate(coordinator, "climate-entry", "unit-1")
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", Mock())
    monkeypatch.setattr(
        climate_module, "schedule_post_write_refresh", Mock()
    )
    monkeypatch.setattr(
        climate_module, "async_auto_exit_sleep_if_needed", AsyncMock()
    )
    return entity, api


@pytest.mark.parametrize(
    ("mode", "option"),
    [("1", "P7"), ("2", "P8"), ("4", "P7")],
)
async def test_set_temperature_routes_to_expected_setpoint(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    option: str,
) -> None:
    """Cooling/heat/auto setpoints reach the proper DKN P channel."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, mode=mode)

    await entity.async_set_temperature(temperature=23)

    api.send_event.assert_awaited_once_with(
        {"event": {"cgi": "modmaquina", "device_id": "unit-1", "option": option, "value": "23.0"}}
    )


@pytest.mark.parametrize(
    ("mode", "option"),
    [("1", "P3"), ("2", "P4"), ("3", "P3"), ("8", "P4"), ("4", "P3")],
)
async def test_set_fan_mode_respects_heat_and_ventilation_alias(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    option: str,
) -> None:
    """Ventilation alias P2=8 uses hot fan P4, not the cold P3 channel."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, mode=mode)

    await entity.async_set_fan_mode("high")

    api.send_event.assert_awaited_once_with(
        {"event": {"cgi": "modmaquina", "device_id": "unit-1", "option": option, "value": "3"}}
    )


@pytest.mark.parametrize("mode", ["5", "6", "7"])
async def test_unsupported_modes_do_not_send_setpoint_or_fan_commands(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """DRY and unmapped telemetry variants never emit unauthorized commands."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, mode=mode)
    await entity.async_set_temperature(temperature=23)
    await entity.async_set_fan_mode("high")
    api.send_event.assert_not_awaited()


@pytest.mark.parametrize(
    ("target", "scenary"),
    [("away", "vacant"), ("sleep", "sleep")],
)
async def test_presets_are_device_put_requests_not_event_commands(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    scenary: str,
) -> None:
    """Preset writes use the canonical PUT /devices/{id} payload."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)

    await entity.async_set_preset_mode(target)

    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": scenary}}
    )
    api.send_event.assert_not_awaited()


async def test_invalid_preset_is_noop(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown preset cannot emit backend writes."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    await entity.async_set_preset_mode("not-a-preset")
    api.put_device_fields.assert_not_awaited()


async def test_power_on_from_off_uses_p1_before_cooling_mode(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn-on emits P1=1; mode changes remain distinct P2 commands."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode="1", power="0"
    )
    await entity.async_turn_on()
    api.send_event.assert_awaited_once_with(
        {"event": {"cgi": "modmaquina", "device_id": "unit-1", "option": "P1", "value": 1}}
    )
