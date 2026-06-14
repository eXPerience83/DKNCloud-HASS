"""Tests for number entity payload shapes."""

from __future__ import annotations

from typing import Any

import pytest


class DummyCoordinator:
    """Minimal coordinator stub exposing Airzone data and hass."""

    def __init__(self, data: dict[str, dict[str, Any]], hass: Any) -> None:
        self.data = data
        self.hass = hass


class DummyAPI:
    """Capture payloads sent to put_device_fields."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def put_device_fields(self, device_id: str, payload: dict[str, Any]) -> None:
        self.calls.append((device_id, payload))


def _make_entity(
    entity_cls: type[Any],
    *,
    device_data: dict[str, Any],
) -> tuple[Any, DummyAPI]:
    entry_id = "entry"
    device_id = "dev1"

    hass = type("DummyHass", (), {"data": {}})()
    hass.data.setdefault("airzoneclouddaikin", {}).setdefault(entry_id, {})[
        "optimistic"
    ] = {}

    coordinator = DummyCoordinator({device_id: device_data}, hass)
    api = DummyAPI()

    entity = entity_cls(
        coordinator=coordinator,
        api=api,
        entry_id=entry_id,
        device_id=device_id,
    )
    entity.hass = hass

    return entity, api


@pytest.mark.asyncio
async def test_sleep_time_payload_is_root_level(
    load_number_module: Any,
) -> None:
    """Sleep time should send a root-level payload."""

    entity, api = _make_entity(
        load_number_module.DKNSleepTimeNumber, device_data={"sleep_time": 30}
    )

    await entity.async_set_native_value(40)

    assert api.calls == [("dev1", {"sleep_time": 40})]


@pytest.mark.asyncio
async def test_unoccupied_heat_min_payload_is_root_level(
    load_number_module: Any,
) -> None:
    """Unoccupied heat min should send a root-level payload."""

    entity, api = _make_entity(
        load_number_module.DKNUnoccupiedHeatMinNumber,
        device_data={"min_temp_unoccupied": 16},
    )

    await entity.async_set_native_value(18)

    assert api.calls == [("dev1", {"min_temp_unoccupied": 18})]


@pytest.mark.asyncio
async def test_unoccupied_cool_max_payload_is_root_level(
    load_number_module: Any,
) -> None:
    """Unoccupied cool max should send a root-level payload."""

    entity, api = _make_entity(
        load_number_module.DKNUnoccupiedCoolMaxNumber,
        device_data={"max_temp_unoccupied": 26},
    )

    await entity.async_set_native_value(28)

    assert api.calls == [("dev1", {"max_temp_unoccupied": 28})]
