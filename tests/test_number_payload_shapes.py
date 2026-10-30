"""Tests for number entity payload shapes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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


@pytest.mark.parametrize(
    ("entity_cls", "field", "initial", "value"),
    [
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 12),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 22),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 28, 24),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 28, 34),
    ],
)
async def test_unoccupied_limit_boundaries_are_written_unchanged(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
    initial: int,
    value: int,
) -> None:
    """Both documented boundaries remain writable without a silent clamp."""
    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )

    await entity.async_set_native_value(value)

    assert api.calls == [("dev1", {field: value})]


@pytest.mark.parametrize(
    ("entity_cls", "field", "initial", "invalid"),
    [
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 11),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 23),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 16.5),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 28, 23),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 28, 35),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 28, 28.5),
    ],
)
async def test_unoccupied_limit_rejects_invalid_writes_without_backend_calls(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
    initial: int,
    invalid: float,
) -> None:
    """Rejected writes produce a translated error and no DKN command."""
    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_set_native_value(invalid)

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "invalid_number_range"
    assert exc_info.value.translation_placeholders == {
        "minimum": str(entity._native_min),
        "maximum": str(entity._native_max),
        "step": "1",
    }
    assert api.calls == []


@pytest.mark.parametrize(
    ("entity_cls", "field", "backend_value"),
    [
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 23.5),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 22.5),
    ],
)
def test_unoccupied_read_keeps_unexpected_backend_values(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
    backend_value: float,
) -> None:
    """Backend telemetry is readable even outside write limits or steps."""
    entity, _api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: backend_value}
    )

    assert entity.native_value == backend_value


@pytest.mark.asyncio
async def test_sleep_timer_keeps_existing_clamp_semantics(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """The unoccupied validation does not alter the independent sleep timer."""
    entity, api = _make_entity(
        hass, dummy_coordinator_factory, DKNSleepTimeNumber, device_data={"sleep_time": 30}
    )

    await entity.async_set_native_value(500)

    assert api.calls == [("dev1", {"sleep_time": 120})]
