"""Tests for diagnostics redaction helpers."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.diagnostics import (
    async_get_config_entry_diagnostics,
)


class DummyCoordinator:
    """Coordinator stub exposing diagnostic payload similar to runtime."""

    def __init__(self) -> None:
        self.last_update_success = True
        self.update_interval = None
        self.data = {
            "device-1": {
                "name": "Living Room",
                "mac": "AA:BB:CC:DD:EE:FF",
                "user_token": "secret-token",
                "latitude": 40.4168,
                "longitude": -3.7038,
                "contactEmail": "owner@example.com",
                "metadata": {"gpsCoord": "40.4168,-3.7038"},
            }
        }


def _assert_redacted(value: object) -> None:
    """Accept HA and local redaction sentinels."""
    assert value in {"***", "**REDACTED**"}


async def test_diagnostics_redacts_sensitive_fields(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Sensitive identifiers must never leak through diagnostics output."""
    entry = dkn_config_entry_factory(
        data={"device_ids": ["dev-1"]},
        options={"user_token": "token-value"},
    )
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": DummyCoordinator()
    }

    result = await async_get_config_entry_diagnostics(hass, entry)

    _assert_redacted(result["entry"]["options"]["user_token"])

    coordinator_result = result["coordinator"]
    if isinstance(coordinator_result, dict):
        devices = coordinator_result.get("devices", {})
        _assert_redacted(devices["device-1"]["user_token"])
        _assert_redacted(devices["device-1"]["mac"])
        _assert_redacted(devices["device-1"]["contactEmail"])
        _assert_redacted(devices["device-1"]["metadata"]["gpsCoord"])
    else:
        assert coordinator_result == "***"

    flattened = str(result)
    assert "secret-token" not in flattened
    assert "AA:BB:CC:DD:EE:FF" not in flattened
    assert "owner@example.com" not in flattened
    assert "40.4168" not in flattened
    assert "-3.7038" not in flattened
    assert '"mac"' not in flattened
    assert '"latitude"' not in flattened


async def test_diagnostics_redacts_extended_pii_fields(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Redaction should cover additional sensitive fields in entries and devices."""
    entry = dkn_config_entry_factory(
        data={
            "installation_id": "install-123",
            "spot_name": "My Home",
            "complete_name": "John Doe",
            "time_zone": "Europe/Madrid",
        },
        options={
            "installation_id": "install-123",
            "time_zone": "Europe/Madrid",
            "spot_name": "My Home",
            "complete_name": "John Doe",
            "user_token": "token-value",
        },
    )
    entry.add_to_hass(hass)

    coordinator = DummyCoordinator()
    coordinator.data["device-1"].update(
        {
            "installation_id": "install-123",
            "spot_name": "My Home",
            "complete_name": "John Doe",
            "time_zone": "Europe/Madrid",
            "device_ids": ["dev-1", "dev-2"],
            "metadata": {"owner_id": "owner-123"},
            "ws_id": "ws-456",
        }
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator}

    result = await async_get_config_entry_diagnostics(hass, entry)

    options = result["entry"]["options"]
    _assert_redacted(options["installation_id"])
    _assert_redacted(options["time_zone"])
    _assert_redacted(options["spot_name"])
    _assert_redacted(options["complete_name"])
    _assert_redacted(options["user_token"])

    coordinator_result = result["coordinator"]
    flattened = str(result)

    if isinstance(coordinator_result, dict):
        device_data = coordinator_result["devices"]["device-1"]
        _assert_redacted(device_data["installation_id"])
        _assert_redacted(device_data["spot_name"])
        _assert_redacted(device_data["complete_name"])
        _assert_redacted(device_data["time_zone"])
        _assert_redacted(device_data["metadata"]["owner_id"])
        assert device_data["ws_id"] == "ws-456"
        assert "ws-456" in flattened
    else:
        assert coordinator_result == "***"
        assert "ws-456" not in flattened

    assert "install-123" not in flattened
    assert "Europe/Madrid" not in flattened
    assert "My Home" not in flattened
    assert "owner-123" not in flattened
