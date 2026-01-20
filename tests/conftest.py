"""Shared pytest fixtures for module stubbing."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stub_ha_and_integration_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Home Assistant and integration modules for isolated imports."""

    def _set_module(name: str, module: types.ModuleType) -> None:
        monkeypatch.setitem(sys.modules, name, module)

    ha_module = types.ModuleType("homeassistant")
    _set_module("homeassistant", ha_module)

    core_module = types.ModuleType("homeassistant.core")

    class HomeAssistant:  # pragma: no cover - type stub only
        """Minimal HomeAssistant stub for type hints."""

        def __init__(self) -> None:
            self.data: dict[str, Any] = {}

    core_module.HomeAssistant = HomeAssistant
    _set_module("homeassistant.core", core_module)
    ha_module.core = core_module

    config_entries_module = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:  # pragma: no cover - type stub only
        """Minimal ConfigEntry stub."""

        entry_id = "entry"

    config_entries_module.ConfigEntry = ConfigEntry
    _set_module("homeassistant.config_entries", config_entries_module)
    ha_module.config_entries = config_entries_module

    number_component = types.ModuleType("homeassistant.components.number")

    class NumberEntity:  # pragma: no cover - base entity stub
        def async_write_ha_state(self) -> None:
            return None

    class NumberMode:  # pragma: no cover - constant stub
        """NumberMode stub."""

        SLIDER = "slider"

    number_component.NumberEntity = NumberEntity
    number_component.NumberMode = NumberMode
    _set_module("homeassistant.components.number", number_component)

    helpers_entity_module = types.ModuleType("homeassistant.helpers.entity")

    class EntityCategory:  # pragma: no cover - constant stub
        """EntityCategory stub."""

        CONFIG = "config"

    helpers_entity_module.EntityCategory = EntityCategory
    _set_module("homeassistant.helpers.entity", helpers_entity_module)

    helpers_device_registry_module = types.ModuleType(
        "homeassistant.helpers.device_registry"
    )

    class DeviceInfo(dict):  # pragma: no cover - minimal stub
        """Dict-based DeviceInfo stub."""

    helpers_device_registry_module.DeviceInfo = DeviceInfo
    helpers_device_registry_module.CONNECTION_NETWORK_MAC = "mac"
    _set_module("homeassistant.helpers.device_registry", helpers_device_registry_module)

    helpers_update_module = types.ModuleType("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:  # pragma: no cover - stub only
        def __init__(self, coordinator: Any) -> None:
            self.coordinator = coordinator

        def __class_getitem__(cls, _item: Any) -> type:
            return cls

    helpers_update_module.CoordinatorEntity = CoordinatorEntity
    _set_module("homeassistant.helpers.update_coordinator", helpers_update_module)

    const_module = types.ModuleType("homeassistant.const")

    class UnitOfTemperature:  # pragma: no cover - constant stub
        """UnitOfTemperature stub."""

        CELSIUS = "°C"

    class UnitOfTime:  # pragma: no cover - constant stub
        """UnitOfTime stub."""

        MINUTES = "min"

    const_module.UnitOfTemperature = UnitOfTemperature
    const_module.UnitOfTime = UnitOfTime
    _set_module("homeassistant.const", const_module)
    ha_module.const = const_module

    custom_components_module = types.ModuleType("custom_components")
    custom_components_module.__path__ = [str(ROOT / "custom_components")]
    _set_module("custom_components", custom_components_module)

    airzone_package = types.ModuleType("custom_components.airzoneclouddaikin")
    airzone_package.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]
    _set_module("custom_components.airzoneclouddaikin", airzone_package)

    airzone_init_stub = types.ModuleType(
        "custom_components.airzoneclouddaikin.__init__"
    )

    class _AirzoneCoordinatorStub:
        """Lightweight AirzoneCoordinator replacement for number imports."""

        def __init__(self) -> None:
            self.data: dict[str, dict[str, Any]] = {}
            self.hass: HomeAssistant | None = None

    airzone_init_stub.AirzoneCoordinator = _AirzoneCoordinatorStub
    _set_module("custom_components.airzoneclouddaikin.__init__", airzone_init_stub)

    airzone_api_stub = types.ModuleType(
        "custom_components.airzoneclouddaikin.airzone_api"
    )

    class AirzoneAPI:  # pragma: no cover - type stub
        """Minimal AirzoneAPI stub."""

    airzone_api_stub.AirzoneAPI = AirzoneAPI
    _set_module("custom_components.airzoneclouddaikin.airzone_api", airzone_api_stub)

    helpers_stub = types.ModuleType("custom_components.airzoneclouddaikin.helpers")

    def acquire_device_lock(*_args: Any, **_kwargs: Any) -> asyncio.Lock:
        return asyncio.Lock()

    def clamp_number(value: float, *_args: Any, **_kwargs: Any) -> float:
        return value

    def optimistic_get(
        _hass: Any, _entry_id: str, _device_id: str, _field: str, backend_value: Any
    ) -> Any:
        return backend_value

    def optimistic_set(*_args: Any, **_kwargs: Any) -> None:
        return None

    def optimistic_invalidate(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def async_auto_exit_sleep_if_needed(*_args: Any, **_kwargs: Any) -> None:
        return None

    def schedule_post_write_refresh(*_args: Any, **_kwargs: Any) -> None:
        return None

    helpers_stub.acquire_device_lock = acquire_device_lock
    helpers_stub.clamp_number = clamp_number
    helpers_stub.optimistic_get = optimistic_get
    helpers_stub.optimistic_set = optimistic_set
    helpers_stub.optimistic_invalidate = optimistic_invalidate
    helpers_stub.async_auto_exit_sleep_if_needed = async_auto_exit_sleep_if_needed
    helpers_stub.schedule_post_write_refresh = schedule_post_write_refresh
    _set_module("custom_components.airzoneclouddaikin.helpers", helpers_stub)


@pytest.fixture
def load_number_module(stub_ha_and_integration_modules: None) -> types.ModuleType:
    """Load the number module under test after stubbing dependencies."""

    module_name = "custom_components.airzoneclouddaikin.number"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "custom_components" / "airzoneclouddaikin" / "number.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
