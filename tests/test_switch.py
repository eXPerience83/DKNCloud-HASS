"""Tests for Airzone power switch proxy vs fallback behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound

from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch


def _make_switch(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    device_snapshot: dict[str, Any],
    *,
    climate_entity_id: str = "climate.device",
) -> AirzonePowerSwitch:
    """Instantiate an AirzonePowerSwitch with a fake coordinator attached."""
    entry_id = "entry"
    device_id = device_snapshot.get("id", "device")
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})["optimistic"] = {}

    coordinator = dummy_coordinator_factory({device_id: device_snapshot})
    entity = AirzonePowerSwitch(coordinator, entry_id, device_id)
    entity.hass = hass
    entity.context = None
    entity.async_write_ha_state = lambda: None
    entity._climate_entity_id = climate_entity_id

    def _fake_resolve(self: AirzonePowerSwitch) -> str | None:
        return self._climate_entity_id

    entity._resolve_climate_entity_id = _fake_resolve.__get__(
        entity,
        AirzonePowerSwitch,
    )

    return entity


def _register_failing_climate_service(
    hass: HomeAssistant, service: str, error: Exception
) -> None:
    """Register a real HA service that raises the requested error."""

    async def failing_service_call(_call: Any) -> None:
        raise error

    hass.services.async_register("climate", service, failing_service_call)


@pytest.mark.asyncio
async def test_turn_on_timeout_keeps_proxy_and_calls_fallback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Timeouts in the climate proxy should not clear the cached entity id."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "0"},
    )

    _register_failing_climate_service(hass, "turn_on", TimeoutError("boom"))
    called = {"fallback": False}

    async def fake_fallback() -> None:
        called["fallback"] = True

    entity._fallback_turn_on = fake_fallback

    await entity.async_turn_on()

    assert entity._climate_entity_id == "climate.device"
    assert called["fallback"] is True


@pytest.mark.asyncio
async def test_turn_on_service_not_found_drops_proxy(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """ServiceNotFound must decouple the proxy and still call the fallback."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "0"},
    )

    _register_failing_climate_service(
        hass, "turn_on", ServiceNotFound("climate", "turn_on")
    )
    called = {"fallback": False}

    async def fake_fallback() -> None:
        called["fallback"] = True

    entity._fallback_turn_on = fake_fallback

    await entity.async_turn_on()

    assert entity._climate_entity_id is None
    assert called["fallback"] is True


@pytest.mark.asyncio
async def test_turn_on_homeassistant_error_keeps_proxy_and_calls_fallback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """HomeAssistantError in the climate proxy should not clear the cached id."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "0"},
    )

    _register_failing_climate_service(
        hass, "turn_on", HomeAssistantError("transient failure")
    )
    called = {"fallback": False}

    async def fake_fallback() -> None:
        called["fallback"] = True

    entity._fallback_turn_on = fake_fallback

    await entity.async_turn_on()

    assert entity._climate_entity_id == "climate.device"
    assert called["fallback"] is True


@pytest.mark.asyncio
async def test_turn_off_unexpected_error_drops_proxy_and_calls_fallback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Unexpected exceptions in the climate proxy should drop the cached id."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "1"},
    )

    _register_failing_climate_service(hass, "turn_off", RuntimeError("unexpected boom"))
    called = {"fallback": False}

    async def fake_fallback() -> None:
        called["fallback"] = True

    entity._fallback_turn_off = fake_fallback

    await entity.async_turn_off()

    assert entity._climate_entity_id is None
    assert called["fallback"] is True


@pytest.mark.asyncio
async def test_send_event_logs_and_reraises_on_failure(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """_send_event should log and re-raise errors from the API client."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "0"},
    )

    class DummyAPI:
        async def send_event(self, _payload: dict[str, Any]) -> None:
            raise RuntimeError("P1 failed")

    entity.coordinator.api = DummyAPI()

    with pytest.raises(RuntimeError, match="P1 failed"):
        await entity._send_event("P1", 1)


def test_backend_power_is_on_normalizes_values(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """_backend_power_is_on should normalize common truthy/falsey values."""
    truthy_values = [True, "true", "on", "yes", 1, "1", 2, "2"]
    falsey_values = [False, "false", "off", "no", 0, "0", "", "none", None]

    for value in truthy_values:
        entity = _make_switch(
            hass,
            dummy_coordinator_factory,
            {"id": "dev1", "name": "Zone", "power": value},
        )
        assert entity._backend_power_is_on() is True

    for value in falsey_values:
        entity = _make_switch(
            hass,
            dummy_coordinator_factory,
            {"id": "dev1", "name": "Zone", "power": value},
        )
        assert entity._backend_power_is_on() is False
