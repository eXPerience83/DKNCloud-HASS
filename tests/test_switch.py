"""Tests for Airzone power switch proxy vs fallback behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers.service import entity_service_call

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
    entity.async_set_context(None)
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
    hass.states.async_set("climate.device", "off")


@pytest.mark.parametrize(
    ("reported_power", "previous_overlay", "method", "command"),
    [
        ("0", "1", "_fallback_turn_off", 0),
        ("1", "0", "_fallback_turn_on", 1),
    ],
)
async def test_switch_fallback_power_intent_wins_over_stale_get(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    monkeypatch: pytest.MonkeyPatch,
    reported_power: str,
    previous_overlay: str,
    method: str,
    command: int,
) -> None:
    """Do not suppress P1 when recent optimistic state contradicts old GET."""
    from custom_components.airzoneclouddaikin.helpers import optimistic_set

    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": reported_power},
        climate_entity_id=None,
    )
    entity.coordinator.api = AsyncMock()
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.switch.schedule_post_write_refresh",
        lambda *_args, **_kwargs: None,
    )
    optimistic_set(hass, "entry", "dev1", "power", previous_overlay)
    await getattr(entity, method)()
    events = [
        call.args[0]["event"]
        for call in entity.coordinator.api.send_event.await_args_list
    ]
    assert len(events) == 1
    assert events[0]["option"] == "P1"
    assert events[0]["value"] == command
    assert entity.is_on is bool(command)


@pytest.mark.parametrize(
    ("initial", "first", "second", "expected", "final"),
    [
        ("1", "_fallback_turn_off", "_fallback_turn_on", [0, 1], True),
        ("0", "_fallback_turn_on", "_fallback_turn_off", [1, 0], False),
        ("1", "_fallback_turn_off", "_fallback_turn_off", [0], False),
        ("0", "_fallback_turn_on", "_fallback_turn_on", [1], True),
    ],
)
async def test_switch_fallback_concurrent_power_intents_are_serialized(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    initial: str,
    first: str,
    second: str,
    expected: list[int],
    final: bool,
) -> None:
    """Fallback must recheck post-lock power instead of replaying pending P1."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": initial},
        climate_entity_id=None,
    )
    entity.coordinator.api = AsyncMock()
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.switch.schedule_post_write_refresh",
        lambda *_args, **_kwargs: None,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    commands: list[int] = []

    async def paused_send(payload: dict[str, Any]) -> None:
        event = payload["event"]
        assert event["option"] == "P1"
        commands.append(event["value"])
        if len(commands) == 1:
            entered.set()
            await release.wait()

    entity.coordinator.api.send_event.side_effect = paused_send
    first_task = asyncio.create_task(getattr(entity, first)())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_task = asyncio.create_task(getattr(entity, second)())
        await asyncio.sleep(0)
        assert not second_task.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=3)

    assert commands == expected
    assert entity.is_on is final


async def test_switch_newer_off_supersedes_on_during_sleep_preparation(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer OFF prevents an older delayed fallback wake from sending P1."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "0"},
        climate_entity_id=None,
    )
    entity.coordinator.api = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def paused_prepare(_reason: str) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(entity, "_ensure_occupied_before_active_action", paused_prepare)
    old_on = asyncio.create_task(entity._fallback_turn_on())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await entity._fallback_turn_off()
    finally:
        release.set()
    await asyncio.wait_for(old_on, timeout=3)

    entity.coordinator.api.send_event.assert_not_awaited()
    assert not entity.is_on


@pytest.mark.parametrize(
    ("service", "power"),
    [("turn_on", "0"), ("turn_off", "1")],
)
@pytest.mark.asyncio
async def test_successful_climate_proxy_does_not_use_p1_fallback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    service: str,
    power: str,
) -> None:
    """Both power commands delegate exactly once when the climate service works."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": power},
    )
    originating_context = Context()
    entity.async_set_context(originating_context)
    delegated: list[str] = []
    propagated_context_ids: list[str] = []
    hass.states.async_set("climate.device", "off")

    async def successful_service(call: Any) -> None:
        delegated.append(call.data["entity_id"])
        propagated_context_ids.append(call.context.id)

    async def unexpected_fallback() -> None:
        pytest.fail("Successful climate delegation must not issue direct P1")

    hass.services.async_register("climate", service, successful_service)
    setattr(entity, f"_fallback_{service}", unexpected_fallback)

    await getattr(entity, f"async_{service}")()

    assert delegated == ["climate.device"]
    assert propagated_context_ids == [originating_context.id]
    assert entity._climate_entity_id == "climate.device"


@pytest.mark.parametrize("climate_state", [None, STATE_UNAVAILABLE])
@pytest.mark.parametrize(
    ("service", "power"),
    [("turn_on", "0"), ("turn_off", "1")],
)
@pytest.mark.asyncio
async def test_inactive_climate_entity_uses_real_p1_fallback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    power: str,
    climate_state: str | None,
) -> None:
    """HA must not silently swallow P1 with absent or restored Climate state."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": power},
    )
    entity.coordinator.api = AsyncMock()
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.switch.schedule_post_write_refresh",
        lambda *_args, **_kwargs: None,
    )
    if climate_state is not None:
        hass.states.async_set("climate.device", climate_state, {"restored": True})

    async def dispatch_climate_service(call: Any) -> None:
        await entity_service_call(hass, {}, f"async_{call.service}", call)

    hass.services.async_register("climate", service, dispatch_climate_service)

    await getattr(entity, f"async_{service}")()

    entity.coordinator.api.send_event.assert_awaited_once_with(
        {
            "event": {
                "cgi": "modmaquina",
                "device_id": "dev1",
                "option": "P1",
                "value": 1 if service == "turn_on" else 0,
            }
        }
    )
    assert entity.is_on is (service == "turn_on")
    assert entity._climate_entity_id == "climate.device"


@pytest.mark.parametrize(
    ("service", "power"),
    [("turn_on", "0"), ("turn_off", "1")],
)
@pytest.mark.asyncio
async def test_missing_climate_proxy_calls_p1_fallback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    service: str,
    power: str,
) -> None:
    """Both power commands still use direct fallback without a climate entity."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": power},
    )
    entity._climate_entity_id = None
    calls: list[str] = []

    async def fake_fallback() -> None:
        calls.append(service)

    setattr(entity, f"_fallback_{service}", fake_fallback)

    await getattr(entity, f"async_{service}")()

    assert calls == [service]
    assert entity._climate_entity_id is None


@pytest.mark.parametrize(
    ("service", "power"),
    [("turn_on", "0"), ("turn_off", "1")],
)
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError,
        HomeAssistantError,
        RuntimeError,
        ServiceNotFound,
    ],
)
async def test_delegated_climate_failure_never_replays_ambiguous_power_command(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    service: str,
    power: str,
    failure: type[Exception],
) -> None:
    """A delegated P1 may have been accepted even if Climate raises afterward."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": power},
    )
    delegated: list[str] = []

    async def accepted_then_failed(_call: Any) -> None:
        delegated.append(service)
        if failure is ServiceNotFound:
            raise ServiceNotFound("climate", service)
        raise failure("after P1 dispatch")

    hass.states.async_set("climate.device", "off")
    hass.services.async_register("climate", service, accepted_then_failed)

    async def unexpected_fallback() -> None:
        pytest.fail("Never replay a potentially accepted delegated P1")

    setattr(entity, f"_fallback_{service}", unexpected_fallback)
    with pytest.raises(failure):
        await getattr(entity, f"async_{service}")()

    assert delegated == [service]
    assert entity._climate_entity_id == "climate.device"


@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_missing_climate_service_delegates_to_direct_p1(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    service: str,
) -> None:
    """An absent service cannot have sent P1, so fallback remains safe."""
    entity = _make_switch(
        hass,
        dummy_coordinator_factory,
        {"id": "dev1", "name": "Zone", "power": "0" if service == "turn_on" else "1"},
    )
    hass.states.async_set("climate.device", "off")
    assert not hass.services.has_service("climate", service)
    delegated: list[str] = []

    async def fallback() -> None:
        delegated.append(service)

    setattr(entity, f"_fallback_{service}", fallback)
    await getattr(entity, f"async_{service}")()

    assert delegated == [service]
    assert entity._climate_entity_id is None


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
