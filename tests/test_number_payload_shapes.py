"""Tests for number entity payload shapes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.number import (
    DKNSleepTimeNumber,
    DKNUnoccupiedCoolMaxNumber,
    DKNUnoccupiedHeatMinNumber,
)


class DummyAPI:
    """Capture payloads sent to put_device_fields."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def put_device_fields(self, device_id: str, payload: dict[str, Any]) -> None:
        """Record a device-field write."""
        self.calls.append((device_id, payload))


@pytest.fixture(autouse=True)
def disable_post_write_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid delayed refresh timers in payload-shape unit tests."""

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.number.schedule_post_write_refresh",
        lambda *_args, **_kwargs: None,
    )


def _make_entity(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    *,
    device_data: dict[str, Any],
) -> tuple[Any, DummyAPI]:
    """Instantiate a number entity with a fake coordinator and API."""
    entry_id = "entry"
    device_id = "dev1"
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})["optimistic"] = {}

    api = DummyAPI()
    coordinator = dummy_coordinator_factory({device_id: device_data}, api)

    entity = entity_cls(
        coordinator=coordinator,
        api=api,
        entry_id=entry_id,
        device_id=device_id,
    )
    entity.hass = hass
    entity.async_write_ha_state = lambda: None

    return entity, api


@pytest.mark.asyncio
async def test_sleep_time_payload_is_root_level(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Sleep time should send a root-level payload."""
    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )

    await entity.async_set_native_value(40)

    assert api.calls == [("dev1", {"sleep_time": 40})]


@pytest.mark.asyncio
async def test_unoccupied_heat_min_payload_is_root_level(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Unoccupied heat min should send a root-level payload."""
    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNUnoccupiedHeatMinNumber,
        device_data={"min_temp_unoccupied": 16},
    )

    await entity.async_set_native_value(18)

    assert api.calls == [("dev1", {"min_temp_unoccupied": 18})]


@pytest.mark.asyncio
async def test_unoccupied_cool_max_payload_is_root_level(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Unoccupied cool max should send a root-level payload."""
    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNUnoccupiedCoolMaxNumber,
        device_data={"max_temp_unoccupied": 26},
    )

    await entity.async_set_native_value(28)

    assert api.calls == [("dev1", {"max_temp_unoccupied": 28})]
