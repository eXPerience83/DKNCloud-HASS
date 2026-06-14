"""Tests for diagnostics redaction helpers."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
components_module = sys.modules.setdefault(
    "homeassistant.components", types.ModuleType("homeassistant.components")
)
ha_module.components = getattr(ha_module, "components", components_module)

diagnostics_module = sys.modules.setdefault(
    "homeassistant.components.diagnostics",
    types.ModuleType("homeassistant.components.diagnostics"),
)

if not hasattr(diagnostics_module, "async_redact_data"):

    def async_redact_data(data: Any, to_redact: set[str]) -> Any:
        """Return a deep copy of data with selected keys redacted."""

        def _walk(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: ("***" if key in to_redact else _walk(val))
                    for key, val in value.items()
                }
            if isinstance(value, list):
                return [_walk(item) for item in value]
            return value

        return _walk(deepcopy(data))

    diagnostics_module.async_redact_data = async_redact_data

config_entries_module = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)

if not hasattr(config_entries_module, "ConfigEntry"):

    class ConfigEntry:  # pragma: no cover - stub only
        pass

    config_entries_module.ConfigEntry = ConfigEntry

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)

if not hasattr(core_module, "HomeAssistant"):

    class HomeAssistant:  # pragma: no cover - stub only
        data: dict[str, Any]

    core_module.HomeAssistant = HomeAssistant

custom_components_module = sys.modules.setdefault(
    "custom_components", types.ModuleType("custom_components")
)
custom_components_module.__path__ = [str(ROOT / "custom_components")]

airzone_package = sys.modules.setdefault(
    "custom_components.airzoneclouddaikin",
    types.ModuleType("custom_components.airzoneclouddaikin"),
)
airzone_package.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]

diagnostics_spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.diagnostics",
    ROOT / "custom_components" / "airzoneclouddaikin" / "diagnostics.py",
)
assert diagnostics_spec is not None and diagnostics_spec.loader is not None

diagnostics_module_impl = importlib.util.module_from_spec(diagnostics_spec)
sys.modules[diagnostics_spec.name] = diagnostics_module_impl
diagnostics_spec.loader.exec_module(diagnostics_module_impl)
async_get_config_entry_diagnostics = (
    diagnostics_module_impl.async_get_config_entry_diagnostics
)

const_module = sys.modules.get("custom_components.airzoneclouddaikin.const")
if const_module is None:
    const_spec = importlib.util.spec_from_file_location(
        "custom_components.airzoneclouddaikin.const",
        ROOT / "custom_components" / "airzoneclouddaikin" / "const.py",
    )
    assert const_spec is not None and const_spec.loader is not None
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

DOMAIN = const_module.DOMAIN


class DummyConfigEntry:
    """Minimal config entry stub for diagnostics testing."""

    def __init__(self) -> None:
        self.entry_id = "test-entry"
        self.title = "DKN Cloud"
        self.data = {"user_email": "user@example.com", "device_ids": ["dev-1"]}
        self.options = {"user_token": "token-value"}
        self.version = 1


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


class DummyHass:
    """Minimal Home Assistant stub that exposes the integration data bucket."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


def test_diagnostics_redacts_sensitive_fields() -> None:
    """Sensitive identifiers must never leak through diagnostics output."""

    hass = DummyHass()
    entry = DummyConfigEntry()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": DummyCoordinator()
    }

    result = asyncio.run(async_get_config_entry_diagnostics(hass, entry))

    assert result["entry"]["options"]["user_token"] == "***"

    coordinator_result = result["coordinator"]
    if isinstance(coordinator_result, dict):
        devices = coordinator_result.get("devices", {})
        assert devices["device-1"]["user_token"] == "***"
        assert devices["device-1"]["mac"] == "***"
        assert devices["device-1"]["contactEmail"] == "***"
        assert devices["device-1"]["metadata"]["gpsCoord"] == "***"
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


def test_diagnostics_redacts_extended_pii_fields() -> None:
    """Redaction should cover additional sensitive fields in entries and devices."""

    hass = DummyHass()
    entry = DummyConfigEntry()
    entry.data.update(
        {
            "installation_id": "install-123",
            "spot_name": "My Home",
            "complete_name": "John Doe",
            "time_zone": "Europe/Madrid",
        }
    )
    entry.options.update(
        {
            "installation_id": "install-123",
            "time_zone": "Europe/Madrid",
            "spot_name": "My Home",
            "complete_name": "John Doe",
        }
    )

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

    result = asyncio.run(async_get_config_entry_diagnostics(hass, entry))

    options = result["entry"]["options"]
    assert options["installation_id"] == "***"
    assert options["time_zone"] == "***"
    assert options["spot_name"] == "***"
    assert options["complete_name"] == "***"
    assert options["user_token"] == "***"

    coordinator_result = result["coordinator"]
    flattened = str(result)

    if isinstance(coordinator_result, dict):
        device_data = coordinator_result["devices"]["device-1"]
        assert device_data["installation_id"] == "***"
        assert device_data["spot_name"] == "***"
        assert device_data["complete_name"] == "***"
        assert device_data["time_zone"] == "***"
        assert device_data["metadata"]["owner_id"] == "***"
        assert device_data["ws_id"] == "ws-456"
        assert "ws-456" in flattened
    else:
        assert coordinator_result == "***"
        assert "ws-456" not in flattened

    assert "install-123" not in flattened
    assert "Europe/Madrid" not in flattened
    assert "My Home" not in flattened
    assert "owner-123" not in flattened
