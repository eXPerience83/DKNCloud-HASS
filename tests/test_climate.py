"""Tests for Airzone climate entity behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
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


@pytest.mark.parametrize("bitmask", ["", "invalid", None])
def test_unknown_mode_capabilities_are_not_offered_for_selection(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    bitmask: str | None,
) -> None:
    """Without a valid bitmask, preserve telemetry but fail closed for selections."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"name": "Zone", "modes": bitmask, "mode": "1", "power": "1"},
        heat_cool_opt_in=True,
    )

    assert entity.hvac_mode is HVACMode.COOL
    assert entity.hvac_modes == [HVACMode.OFF]


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


@pytest.mark.parametrize("reported_mode", ["6", "7"])
@pytest.mark.parametrize("modes_bitmask", ["11101", ""])
def test_reported_mode_aliases_show_heat_cool_without_enabling_writes(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    reported_mode: str,
    modes_bitmask: str,
) -> None:
    """Backend-only aliases remain displayable with HEAT_COOL opt-in disabled."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"name": "Zone", "modes": modes_bitmask, "mode": reported_mode, "power": "1"},
        heat_cool_opt_in=False,
    )

    assert entity.hvac_mode is HVACMode.HEAT_COOL
    assert HVACMode.HEAT_COOL not in entity.hvac_modes
    assert entity._heat_cool_enabled() is False
    assert not entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert not entity.supported_features & ClimateEntityFeature.FAN_MODE


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


def test_heat_cool_not_selectable_when_current_mode_loses_support(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Read a reported HEAT_COOL without advertising unsupported P2=4 writes."""
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
    ]
    assert entity.hvac_mode is HVACMode.HEAT_COOL


@pytest.mark.parametrize("reported_mode", ["4", "6", "7"])
def test_heat_cool_reported_state_does_not_override_disabled_opt_in(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    reported_mode: str,
) -> None:
    """Even a supported P2=4 remains unselectable until explicitly opted in."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"modes": "11111", "mode": reported_mode, "power": "1"},
        heat_cool_opt_in=False,
    )

    assert entity.hvac_mode is HVACMode.HEAT_COOL
    assert HVACMode.HEAT_COOL not in entity.hvac_modes


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


@pytest.mark.parametrize(
    ("reported_mode", "setpoint", "minimum", "maximum", "fan"),
    [
        ("6", 25.0, 18.0, 30.0, "medium"),
        ("7", 21.0, 16.0, 27.0, "high"),
    ],
)
def test_auto_telemetry_aliases_use_correct_cold_or_heat_fields(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    reported_mode: str,
    setpoint: float,
    minimum: float,
    maximum: float,
    fan: str,
) -> None:
    """DKN APK uses P7/P3 fields for 6, but P8/P4 fields for 7."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {
            "modes": "11101000",
            "power": "1",
            "mode": reported_mode,
            "cold_consign": "25",
            "heat_consign": "21",
            "min_limit_cold": "18",
            "max_limit_cold": "30",
            "min_limit_heat": "16",
            "max_limit_heat": "27",
            "cold_speed": "2",
            "heat_speed": "3",
            "availables_speeds": "3",
        },
        heat_cool_opt_in=False,
    )

    assert entity.hvac_mode is HVACMode.HEAT_COOL
    assert HVACMode.HEAT_COOL not in entity.hvac_modes
    assert entity.target_temperature == setpoint
    assert entity.min_temp == minimum
    assert entity.max_temp == maximum
    assert entity.fan_mode == fan
    assert not entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert not entity.supported_features & ClimateEntityFeature.FAN_MODE


@pytest.mark.parametrize("opt_in", [False, True])
def test_real_device_bitmask_never_offers_auto_or_alias_modes(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    opt_in: bool,
) -> None:
    """User's device advertises P2=1,2,3,5; never P2=4/6/7/8."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"modes": "11101000", "power": "1", "mode": "2"},
        heat_cool_opt_in=opt_in,
    )

    assert entity.hvac_modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]


def test_unapproved_reported_mode_4_remains_read_only(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Backend telemetry alone is not permission to issue AUTO control writes."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"modes": "11101000", "power": "1", "mode": "4"},
        heat_cool_opt_in=False,
    )

    assert entity.hvac_mode is HVACMode.HEAT_COOL
    assert HVACMode.HEAT_COOL not in entity.hvac_modes
    assert not entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert not entity.supported_features & ClimateEntityFeature.FAN_MODE


@pytest.mark.parametrize(
    ("modes_bitmask", "is_supported"),
    [
        ("00100000", True),  # P2=3
        ("00000001", False),  # P2=8 is read-only
        ("00100001", True),  # P2=3 supported; never write P2=8
        ("11000000", False),  # neither
        ("", False),  # unknown capabilities: never offer an unverified write
    ],
)
def test_fan_only_is_exposed_only_for_advertised_p2_codes(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    modes_bitmask: str,
    is_supported: bool,
) -> None:
    """Only P2=3 is selectable; P2=8 is a backend telemetry alias."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"power": "1", "mode": "1", "modes": modes_bitmask},
        heat_cool_opt_in=False,
    )

    assert (HVACMode.FAN_ONLY in entity.hvac_modes) is is_supported


@pytest.mark.parametrize("modes_bitmask", ["00000001", "11000000", ""])
def test_reported_mode_8_is_fan_only_regardless_of_bitmask(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    modes_bitmask: str,
) -> None:
    """Backend FAN_ONLY mode 8 remains readable even with missing capabilities."""
    entity = _make_climate(
        hass,
        dummy_coordinator_factory,
        {"power": "1", "mode": "8", "modes": modes_bitmask},
        heat_cool_opt_in=False,
    )

    assert entity.hvac_mode is HVACMode.FAN_ONLY
