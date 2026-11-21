"""Deterministic DKN climate write contract tests: no real devices or cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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
    modes: str = "11111",
    heat_cool_opt_in: bool = True,
) -> tuple[AirzoneClimate, Any]:
    """Create an independent climate entity backed by a synthetic DKN device."""
    dev = {
        "id": "unit-1",
        "name": "Test Zone",
        "mode": mode,
        "power": power,
        "scenary": scenary,
        "modes": modes,
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
    ] = heat_cool_opt_in
    coordinator = dummy_coordinator_factory({"unit-1": dev}, api=api)
    hass.data[DOMAIN]["climate-entry"]["coordinator"] = coordinator
    entity = AirzoneClimate(coordinator, "climate-entry", "unit-1")
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", Mock())
    refresh = Mock()
    monkeypatch.setattr(climate_module, "schedule_post_write_refresh", refresh)
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
        refresh,
    )
    monkeypatch.setattr(climate_module, "async_auto_exit_sleep_if_needed", AsyncMock())
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
        {
            "event": {
                "cgi": "modmaquina",
                "device_id": "unit-1",
                "option": option,
                "value": "23.0",
            }
        }
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
        {
            "event": {
                "cgi": "modmaquina",
                "device_id": "unit-1",
                "option": option,
                "value": "3",
            }
        }
    )


@pytest.mark.parametrize(
    ("mode", "setter", "option", "key"),
    [
        ("1", "temperature", "P7", "cold_consign"),
        ("2", "temperature", "P8", "heat_consign"),
        ("1", "fan", "P3", "cold_speed"),
        ("2", "fan", "P4", "heat_speed"),
    ],
)
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_climate_nonpower_ambiguous_p_write_requests_recovery(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    setter: str,
    option: str,
    key: str,
    failure: type[BaseException],
) -> None:
    """Do not replay unknown P3/P4/P7/P8 outcomes; request readback."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode=mode, power="1"
    )
    api.send_event.side_effect = failure("unknown control result")
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(failure):
        if setter == "temperature":
            await entity.async_set_temperature(temperature=23)
        else:
            await entity.async_set_fan_mode("high")

    events = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [event["option"] for event in events] == [option]
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    assert key not in hass.data[DOMAIN]["climate-entry"].get("optimistic", {}).get(
        "unit-1", {}
    )
    entity.async_write_ha_state.assert_not_called()


async def test_climate_known_unsent_setpoint_does_not_request_recovery(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cooldown path made no P7 send and needs no corrective GET."""
    from custom_components.airzoneclouddaikin.airzone_api import (
        CommandNotSentCooldown,
    )

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="1")
    api.send_event.side_effect = CommandNotSentCooldown(hass.loop.time() + 60)
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_temperature(temperature=23)

    manager.request_recovery.assert_not_called()


@pytest.mark.parametrize(
    ("initial_mode", "next_mode"),
    [("1", HVACMode.HEAT), ("2", HVACMode.COOL)],
)
@pytest.mark.parametrize("setter", ["temperature", "fan"])
async def test_mode_change_supersedes_queued_setpoint_and_fan_route(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    initial_mode: str,
    next_mode: HVACMode,
    setter: str,
) -> None:
    """No old P7/P8 or P3/P4 command may follow a newer completed P2."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode=initial_mode, power="1"
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    async def held_p2(payload: dict[str, Any], **_kwargs) -> None:
        option = payload["event"]["option"]
        sent.append(option)
        if option == "P2":
            entered.set()
            await release.wait()

    api.send_event.side_effect = held_p2
    mode_change = asyncio.create_task(entity.async_set_hvac_mode(next_mode))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if setter == "temperature":
            queued = asyncio.create_task(entity.async_set_temperature(temperature=23))
        else:
            queued = asyncio.create_task(entity.async_set_fan_mode("high"))
        await asyncio.sleep(0)
        assert not queued.done()
    finally:
        release.set()

    await asyncio.wait_for(asyncio.gather(mode_change, queued), timeout=3)
    assert sent == ["P2"]


async def test_fan_only_alias_change_while_queued_does_not_use_old_channel(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 alias 3->8 keeps FAN_ONLY but changes fan routing P3->P4."""
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode="3", power="1"
    )
    lock = acquire_device_lock(hass, "climate-entry", "unit-1")
    await lock.acquire()
    try:
        task = asyncio.create_task(entity.async_set_fan_mode("high"))
        await asyncio.sleep(0)
        assert not task.done()
        entity.coordinator.data["unit-1"]["mode"] = "8"
    finally:
        lock.release()

    await asyncio.wait_for(task, timeout=3)
    api.send_event.assert_not_awaited()


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


@pytest.mark.parametrize(
    ("initial_scenary", "preset", "expected_scenary"),
    [
        ("occupied", "away", "vacant"),
        ("occupied", "sleep", "sleep"),
        ("vacant", "home", "occupied"),
    ],
)
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_ambiguous_ordinary_preset_requests_readback_without_claiming_success(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    initial_scenary: str,
    preset: str,
    expected_scenary: str,
    failure: type[BaseException],
) -> None:
    """An uncertain ordinary scene PUT gets readback but no Sleep exit fence."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        scenary=initial_scenary,
        power="1",
    )
    manager = Mock()
    bucket = hass.data[DOMAIN]["climate-entry"]
    bucket["sync_manager"] = manager
    api.put_device_fields.side_effect = failure("scene result unknown")

    with pytest.raises(failure):
        await entity.async_set_preset_mode(preset)

    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": expected_scenary}}
    )
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    assert "scenary" not in bucket.get("optimistic", {}).get("unit-1", {})
    assert not bucket.get("sleep_exit_pending")
    assert entity.preset_mode == ("home" if initial_scenary == "occupied" else "away")
    entity.async_write_ha_state.assert_not_called()


async def test_known_unsent_ordinary_preset_does_not_trigger_readback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vendor cooldown rejected the scene request before any network send."""
    from custom_components.airzoneclouddaikin.airzone_api import CommandNotSentCooldown

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager
    api.put_device_fields.side_effect = CommandNotSentCooldown(hass.loop.time() + 60)

    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_preset_mode("away")

    api.put_device_fields.assert_awaited_once()
    manager.request_recovery.assert_not_called()
    assert entity.preset_mode == "home"


async def test_ordinary_preset_timeout_without_manager_uses_legacy_refresh(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without SyncManager the existing scheduled refresh handles uncertainty."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    api.put_device_fields.side_effect = TimeoutError("backend acceptance unknown")

    with pytest.raises(TimeoutError):
        await entity.async_set_preset_mode("away")

    climate_module.schedule_post_write_refresh.assert_called_once_with(
        hass, entity.coordinator, entry_id="climate-entry"
    )
    assert entity.preset_mode == "home"


@pytest.mark.parametrize("unload_status", ["removed", "closing"])
async def test_late_ordinary_preset_failure_does_not_recreate_unloaded_entry(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    unload_status: str,
) -> None:
    """A late HTTP error after unload must not start fresh recovery work."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    manager = Mock()
    bucket = hass.data[DOMAIN]["climate-entry"]
    bucket["sync_manager"] = manager

    async def delayed_put(_device_id: str, _payload: dict[str, Any]) -> None:
        started.set()
        await release.wait()
        raise TimeoutError("PUT response lost after unload")

    api.put_device_fields.side_effect = delayed_put
    task = asyncio.create_task(entity.async_set_preset_mode("away"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        if unload_status == "removed":
            hass.data[DOMAIN].pop("climate-entry")
        else:
            bucket["closing"] = True
    finally:
        release.set()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=3)

    manager.request_recovery.assert_not_called()
    climate_module.schedule_post_write_refresh.assert_not_called()
    if unload_status == "removed":
        assert "climate-entry" not in hass.data[DOMAIN]
    else:
        assert bucket["closing"] is True


@pytest.mark.parametrize("unload_status", ["removed", "closing", "replaced"])
async def test_late_successful_ordinary_preset_does_not_publish_after_unload(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    unload_status: str,
) -> None:
    """A late accepted PUT must not recreate or update an unloaded entry."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = Mock()
    bucket["sync_manager"] = manager
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_put(_device_id: str, _payload: dict[str, Any]) -> None:
        entered.set()
        await release.wait()

    api.put_device_fields.side_effect = held_put
    task = asyncio.create_task(entity.async_set_preset_mode("away"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if unload_status == "removed":
            hass.data[DOMAIN].pop("climate-entry")
        elif unload_status == "replaced":
            hass.data[DOMAIN]["climate-entry"] = {"sync_manager": Mock()}
        else:
            bucket["closing"] = True
    finally:
        release.set()

    await asyncio.wait_for(task, timeout=3)
    api.put_device_fields.assert_awaited_once()
    manager.request_recovery.assert_not_called()
    climate_module.schedule_post_write_refresh.assert_not_called()
    assert "scenary" not in bucket.get("optimistic", {}).get("unit-1", {})
    entity.async_write_ha_state.assert_not_called()
    if unload_status == "removed":
        assert "climate-entry" not in hass.data[DOMAIN]
    elif unload_status == "replaced":
        hass.data[DOMAIN]["climate-entry"][
            "sync_manager"
        ].request_recovery.assert_not_called()


@pytest.mark.parametrize("unload_status", ["removed", "closing"])
async def test_unload_after_first_rearm_put_prevents_second_put(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    unload_status: str,
) -> None:
    """An accepted first Home step must not launch a new Sleep PUT after unload."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    _arm_current_sleep_off_evidence(hass)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_first_put(
        _device_id: str, _payload: dict[str, Any], **_kwargs: Any
    ) -> None:
        entered.set()
        await release.wait()

    api.put_device_fields.side_effect = held_first_put
    task = asyncio.create_task(entity.async_set_preset_mode("sleep"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if unload_status == "removed":
            hass.data[DOMAIN].pop("climate-entry")
        else:
            bucket["closing"] = True
    finally:
        release.set()

    await asyncio.wait_for(task, timeout=3)
    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": "occupied"}}, retry=False
    )
    assert "scenary" not in bucket.get("optimistic", {}).get("unit-1", {})
    entity.async_write_ha_state.assert_not_called()


@pytest.mark.parametrize("unload_status", ["removed", "closing"])
async def test_late_second_rearm_failure_does_not_restore_unloaded_entry(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    unload_status: str,
) -> None:
    """Partial-rearm recovery must not publish Home after the entry has gone."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    _arm_current_sleep_off_evidence(hass)
    _confirm_rearm_home_readback(entity)
    manager = Mock()
    bucket["sync_manager"] = manager
    entered, release = asyncio.Event(), asyncio.Event()

    async def second_put(
        _device_id: str, _payload: dict[str, Any], **_kwargs: Any
    ) -> None:
        if api.put_device_fields.await_count == 2:
            entered.set()
            await release.wait()
            raise TimeoutError("second response unknown")

    api.put_device_fields.side_effect = second_put
    task = asyncio.create_task(entity.async_set_preset_mode("sleep"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if unload_status == "removed":
            hass.data[DOMAIN].pop("climate-entry")
        else:
            bucket["closing"] = True
    finally:
        release.set()

    with pytest.raises(TimeoutError, match="second response unknown"):
        await asyncio.wait_for(task, timeout=3)
    assert api.put_device_fields.await_count == 2
    assert "scenary" not in bucket.get("optimistic", {}).get("unit-1", {})
    manager.request_recovery.assert_not_called()
    entity.async_write_ha_state.assert_not_called()
    if unload_status == "removed":
        assert "climate-entry" not in hass.data[DOMAIN]


@pytest.mark.parametrize("method", ["turn_on", "set_hvac_mode"])
@pytest.mark.parametrize("entry_state", ["removed", "closing", "replaced"])
async def test_queued_wake_does_not_resurrect_unloaded_entry(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    entry_state: str,
) -> None:
    """A queued wake must not create a bucket or command an old HA entity."""
    from custom_components.airzoneclouddaikin.helpers import (
        acquire_device_lock,
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    monkeypatch.setattr(
        climate_module,
        "async_auto_exit_sleep_if_needed",
        async_auto_exit_sleep_if_needed,
    )
    domain_bucket = hass.data[DOMAIN]
    original_bucket = domain_bucket["climate-entry"]
    lock = acquire_device_lock(hass, "climate-entry", "unit-1")
    await lock.acquire()
    if method == "turn_on":
        task = asyncio.create_task(entity.async_turn_on())
    else:
        task = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        if entry_state == "removed":
            domain_bucket.pop("climate-entry")
        elif entry_state == "closing":
            original_bucket["closing"] = True
        else:
            domain_bucket["climate-entry"] = {"replacement": True}
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=3)
    api.send_event.assert_not_awaited()
    api.put_device_fields.assert_not_awaited()
    if entry_state == "removed":
        assert "climate-entry" not in domain_bucket
    elif entry_state == "replaced":
        assert domain_bucket["climate-entry"] == {"replacement": True}
    else:
        assert original_bucket["closing"] is True
        assert "optimistic" not in original_bucket


@pytest.mark.parametrize("method", ["turn_on", "set_hvac_mode"])
async def test_stale_wake_called_after_unload_cannot_create_bucket(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """No power intent or lock may create a bucket for an unloaded entry."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    hass.data[DOMAIN].pop("climate-entry")
    if method == "turn_on":
        await entity.async_turn_on()
    else:
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert "climate-entry" not in hass.data[DOMAIN]
    api.send_event.assert_not_awaited()
    api.put_device_fields.assert_not_awaited()


@pytest.mark.parametrize("unload_status", ["removed", "closing"])
async def test_late_auto_sleep_cleanup_success_does_not_publish_after_unload(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    unload_status: str,
) -> None:
    """An awaited automatic Sleep exit cannot revive an unloaded entry."""
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    entity.coordinator.data["unit-1"]["sleep_off_confirmed"] = True
    _arm_current_sleep_off_evidence(hass)
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = Mock()
    bucket["sync_manager"] = manager
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_home(*_args: Any, **_kwargs: Any) -> None:
        entered.set()
        await release.wait()

    api.async_set_scenary = AsyncMock(side_effect=held_home)
    task = asyncio.create_task(
        async_auto_exit_sleep_if_needed(
            hass,
            entry_id="climate-entry",
            device_id="unit-1",
            device=entity._device,
            coordinator=entity.coordinator,
            reason="expiry",
            is_device_on=lambda: True,
            allow_away_handling=False,
            on_success=entity.async_write_ha_state,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if unload_status == "removed":
            hass.data[DOMAIN].pop("climate-entry")
        else:
            bucket["closing"] = True
    finally:
        release.set()

    await asyncio.wait_for(task, timeout=3)
    api.async_set_scenary.assert_awaited_once()
    assert api.async_set_scenary.await_args.args == ("unit-1", "occupied")
    assert api.async_set_scenary.await_args.kwargs["retry"] is False
    assert callable(api.async_set_scenary.await_args.kwargs["on_start"])
    assert "scenary" not in bucket.get("optimistic", {}).get("unit-1", {})
    manager.request_recovery.assert_not_called()
    entity.async_write_ha_state.assert_not_called()
    if unload_status == "removed":
        assert "climate-entry" not in hass.data[DOMAIN]


async def test_climate_exposes_sleep_overdue_as_reported_diagnostic(
    hass, dummy_coordinator_factory, monkeypatch
):
    entity, _api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    entity.coordinator.data["unit-1"].update(
        sleep_start_known=True,
        sleep_overdue=True,
    )
    assert entity.extra_state_attributes == {
        "sleep_start_known": True,
        "sleep_overdue": True,
    }


async def test_expired_sleep_home_writes_real_backend_cleanup(
    hass, dummy_coordinator_factory, monkeypatch
):
    """Effective Home must not swallow an explicit cleanup of raw Sleep."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    assert entity.preset_mode == "sleep"
    await entity.async_set_preset_mode("home")
    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": "occupied"}}, retry=False
    )
    # An accepted optimistic Home should not trigger a redundant second PUT.
    await entity.async_set_preset_mode("home")
    assert api.put_device_fields.await_count == 1


async def test_expired_sleep_rearms_via_occupied_then_sleep(
    hass, dummy_coordinator_factory, monkeypatch
):
    """A second one-shot session requires two ordered, non-retried PUTs."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import SleepTracking

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    old_start = dt_util.utcnow() - timedelta(hours=2)
    tracking = SleepTracking(
        last_scenary="sleep",
        sleep_started_at_utc=old_start,
        cleanup_started_at_utc=old_start,
        expected_minutes=5,
        expiry_off_reports=2,
        expiry_power_revision=0,
        expiry_verified=True,
        force_exit_requested=True,
    )
    hass.data[DOMAIN]["climate-entry"]["sleep_tracking"] = {"unit-1": tracking}
    _confirm_rearm_home_readback(entity)
    await entity.async_set_preset_mode("sleep")
    assert [
        (call.args, call.kwargs) for call in api.put_device_fields.await_args_list
    ] == [
        (("unit-1", {"device": {"scenary": "occupied"}}), {"retry": False}),
        (("unit-1", {"device": {"scenary": "sleep"}}), {"retry": False}),
    ]
    assert tracking.sleep_started_at_utc is not None
    assert tracking.sleep_started_at_utc > old_start
    assert tracking.last_scenary == "sleep"
    assert not tracking.force_exit_requested
    assert tracking.session_revision == 1
    assert entity.preset_mode == "sleep"


async def test_second_sleep_rearm_failure_preserves_home_and_requests_readback(
    hass, dummy_coordinator_factory, monkeypatch
):
    """Do not claim Sleep activation after an ambiguous second PUT."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    _arm_current_sleep_off_evidence(hass)
    _confirm_rearm_home_readback(entity)
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager
    api.put_device_fields.side_effect = [None, TimeoutError("ambiguous")]
    with pytest.raises(TimeoutError, match="ambiguous"):
        await entity.async_set_preset_mode("sleep")
    assert api.put_device_fields.await_count == 2
    assert entity.preset_mode == "home"
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    assert (
        hass.data[DOMAIN]["climate-entry"]["sleep_exit_pending"]["unit-1"]
        == "unverified"
    )
    # Failed second PUT cannot certify a new session. The original record
    # remains pending fresh backend scene confirmation.
    record = hass.data[DOMAIN]["climate-entry"]["sleep_tracking"]["unit-1"]
    assert record.expiry_verified
    assert record.session_revision == 0


async def test_later_queued_preset_runs_after_earlier_write_commits(
    hass, dummy_coordinator_factory, monkeypatch
):
    """The second user command wins after the first PUT finishes."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def write_scene(_device_id, payload, **_kwargs):
        if payload["device"]["scenary"] == "vacant":
            first_started.set()
            await release.wait()
        return {}

    api.put_device_fields.side_effect = write_scene
    first = asyncio.create_task(entity.async_set_preset_mode("away"))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        second = asyncio.create_task(entity.async_set_preset_mode("sleep"))
        await asyncio.sleep(0)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert [
        call.args[1]["device"]["scenary"]
        for call in api.put_device_fields.await_args_list
    ] == ["vacant", "sleep"]
    assert entity.preset_mode == "sleep"


async def test_automatic_cleanup_finishes_before_newer_manual_sleep(
    hass, dummy_coordinator_factory, monkeypatch
):
    """A queued manual Sleep must survive the cleanup's optimistic Home."""
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    entity.coordinator.data["unit-1"].update(
        sleep_off_confirmed=True,
    )
    # A bare coordinator flag is unsafe; use a verified expiry fixture.
    _arm_current_sleep_off_evidence(hass)
    _confirm_rearm_home_readback(entity)
    entered, release = asyncio.Event(), asyncio.Event()
    api.async_set_scenary = AsyncMock()

    async def delayed_home(*_args, **_kwargs):
        entered.set()
        await release.wait()

    api.async_set_scenary.side_effect = delayed_home
    cleanup = asyncio.create_task(
        async_auto_exit_sleep_if_needed(
            hass,
            entry_id="climate-entry",
            device_id="unit-1",
            device=entity._device,
            coordinator=entity.coordinator,
            reason="expiry",
            is_device_on=lambda: True,
            allow_away_handling=False,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    manual_sleep = asyncio.create_task(entity.async_set_preset_mode("sleep"))
    await asyncio.sleep(0)
    # Repeating the same raw Sleep while cleanup awaits a response is
    # idempotent; it must not queue a new timer or bypass scene readback.
    assert manual_sleep.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(cleanup, manual_sleep), timeout=3)

    api.async_set_scenary.assert_awaited_once()
    assert api.async_set_scenary.await_args.args == ("unit-1", "occupied")
    assert api.async_set_scenary.await_args.kwargs["retry"] is False
    assert callable(api.async_set_scenary.await_args.kwargs["on_start"])
    api.put_device_fields.assert_not_awaited()
    assert entity.preset_mode == "home"
    # The automatic Home path schedules a one-second coordinator refresh
    # without a live SyncManager. The official HA harness requires this
    # fixture-owned timer to be cancelled at test teardown.
    if cancel := hass.data[DOMAIN]["climate-entry"].get("pending_refresh"):
        cancel()


def _arm_current_sleep_off_evidence(hass) -> None:
    """Give expiry tests synthetic post-deadline OFF evidence."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import SleepTracking

    bucket = hass.data[DOMAIN]["climate-entry"]
    coordinator = bucket["coordinator"]
    coordinator.data["unit-1"].update(
        sleep_time=5,
        sleep_expiry_verified=True,
        sleep_off_confirmed=True,
    )
    started = dt_util.utcnow() - timedelta(minutes=6)
    bucket["sleep_tracking"] = {
        "unit-1": SleepTracking(
            last_scenary="sleep",
            sleep_started_at_utc=started,
            cleanup_started_at_utc=started,
            start_known=True,
            expected_minutes=5,
            expiry_verified=True,
            expiry_off_reports=2,
            expiry_power_revision=0,
        )
    }


def _confirm_rearm_home_readback(entity) -> None:
    """Mock an independent occupied GET after the first rearm PUT."""

    async def refresh():
        entity.coordinator.data["unit-1"]["scenary"] = "occupied"
        entity.hass.data[DOMAIN]["climate-entry"].get("sleep_exit_pending", {}).pop(
            "unit-1", None
        )

    entity.coordinator.async_request_refresh = AsyncMock(side_effect=refresh)


async def test_newer_manual_intent_cancels_queued_automatic_cleanup(
    hass, dummy_coordinator_factory, monkeypatch
):
    """The cleanup must not send Home after a newer preset has queued."""
    from custom_components.airzoneclouddaikin.helpers import (
        acquire_device_lock,
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    entity.coordinator.data["unit-1"].update(
        sleep_off_confirmed=True,
    )
    api.async_set_scenary = AsyncMock()
    _arm_current_sleep_off_evidence(hass)
    _confirm_rearm_home_readback(entity)
    lock = acquire_device_lock(hass, "climate-entry", "unit-1")
    await lock.acquire()
    try:
        cleanup = asyncio.create_task(
            async_auto_exit_sleep_if_needed(
                hass,
                entry_id="climate-entry",
                device_id="unit-1",
                device=entity._device,
                coordinator=entity.coordinator,
                reason="expiry",
                is_device_on=lambda: True,
                allow_away_handling=False,
            )
        )
        await asyncio.sleep(0)
        manual = asyncio.create_task(entity.async_set_preset_mode("sleep"))
        await asyncio.sleep(0)
    finally:
        lock.release()
    await asyncio.wait_for(asyncio.gather(cleanup, manual), timeout=3)
    api.async_set_scenary.assert_not_awaited()
    assert api.put_device_fields.await_count == 2
    assert entity.preset_mode == "sleep"


async def test_ambiguous_first_home_step_blocks_automatic_sleep_cleanup(
    hass, dummy_coordinator_factory, monkeypatch
):
    """A timed-out first rearm PUT must not be replayed by expiry cleanup."""
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    entity.coordinator.data["unit-1"].update(
        sleep_off_confirmed=True,
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    _arm_current_sleep_off_evidence(hass)
    api.async_set_scenary = AsyncMock()
    manager = Mock()
    bucket["sync_manager"] = manager
    api.put_device_fields.side_effect = TimeoutError("ambiguous first PUT")
    with pytest.raises(TimeoutError, match="ambiguous first PUT"):
        await entity.async_set_preset_mode("sleep")

    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": "occupied"}}, retry=False
    )
    assert bucket["sleep_exit_pending"]["unit-1"] == "unverified"
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    await async_auto_exit_sleep_if_needed(
        hass,
        entry_id="climate-entry",
        device_id="unit-1",
        device=entity._device,
        coordinator=entity.coordinator,
        reason="expiry",
        is_device_on=lambda: True,
        allow_away_handling=False,
    )
    assert api.put_device_fields.await_count == 1
    api.async_set_scenary.assert_not_awaited()


@pytest.mark.parametrize("preset", ["home", "away"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_ambiguous_explicit_home_away_blocks_old_sleep_cleanup(
    hass, dummy_coordinator_factory, monkeypatch, preset, failure
):
    """A timed-out or cancelled explicit scene must fence later automatic Home."""
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    _arm_current_sleep_off_evidence(hass)
    api.async_set_scenary = AsyncMock()
    manager = Mock()
    bucket["sync_manager"] = manager
    api.put_device_fields.side_effect = failure()

    with pytest.raises(failure):
        await entity.async_set_preset_mode(preset)

    expected = "occupied" if preset == "home" else "vacant"
    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": expected}}, retry=False
    )
    assert bucket["sleep_exit_pending"]["unit-1"] == "unverified"
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)

    # Even if old Sleep OFF telemetry is still cached, the later automatic
    # cleanup must not overwrite the possibly applied manual Home/Away.
    await async_auto_exit_sleep_if_needed(
        hass,
        entry_id="climate-entry",
        device_id="unit-1",
        device=entity._device,
        coordinator=entity.coordinator,
        reason="expiry",
        is_device_on=lambda: True,
        allow_away_handling=False,
    )
    api.async_set_scenary.assert_not_awaited()


@pytest.mark.parametrize("preset", ["home", "away"])
async def test_proven_unsent_explicit_home_away_does_not_fence(
    hass, dummy_coordinator_factory, monkeypatch, preset
):
    """Cooldown rejected before I/O must not create an ambiguous write fence."""
    from custom_components.airzoneclouddaikin.airzone_api import (
        CommandNotSentCooldown,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = Mock()
    bucket["sync_manager"] = manager
    api.put_device_fields.side_effect = CommandNotSentCooldown(hass.loop.time() + 300)

    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_preset_mode(preset)
    assert not bucket.get("sleep_exit_pending")
    manager.request_recovery.assert_not_called()


@pytest.mark.parametrize("preset", ["home", "away"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError, OSError])
async def test_sleep_on_to_off_during_ambiguous_manual_exit(
    hass, dummy_coordinator_factory, monkeypatch, preset, failure
):
    """An in-flight manual Sleep exit must outrank newly confirmed OFF cleanup."""
    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import (
        SleepTracking,
        _derive_state,
        helpers,
    )
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="1"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    bucket["sleep_tracking"] = {
        "unit-1": SleepTracking(
            last_scenary="sleep",
            sleep_started_at_utc=dt_util.utcnow(),
            observed_on=True,
        )
    }
    manager = Mock()
    bucket["sync_manager"] = manager
    api.async_set_scenary = AsyncMock()
    write_entered = asyncio.Event()
    release_write = asyncio.Event()
    cleanup_queued = asyncio.Event()

    async def pending_scene_put(_identity, _payload, **_kwargs):
        write_entered.set()
        await release_write.wait()
        raise failure("unknown outcome")

    api.put_device_fields.side_effect = pending_scene_put
    manual = asyncio.create_task(entity.async_set_preset_mode(preset))
    cleanup = None
    try:
        await asyncio.wait_for(write_entered.wait(), timeout=2)
        # Realistic OFF telemetry arrives while the manual PUT remains pending.
        # Two distinct fresh observations satisfy the active-session gate.
        entity.coordinator.data["unit-1"]["power"] = "0"
        entry = SimpleNamespace(entry_id="climate-entry", options={})
        for _ in range(2):
            _derive_state(
                hass,
                entry,
                entity.coordinator.data,
                fresh_power_ids={"unit-1"},
                fresh_scenary_ids=set(),
            )
        assert entity.coordinator.data["unit-1"]["sleep_off_confirmed"]

        original_acquire = helpers.acquire_device_lock

        def observed_lock(*args, **kwargs):
            cleanup_queued.set()
            return original_acquire(*args, **kwargs)

        monkeypatch.setattr(helpers, "acquire_device_lock", observed_lock)
        cleanup = asyncio.create_task(
            async_auto_exit_sleep_if_needed(
                hass,
                entry_id="climate-entry",
                device_id="unit-1",
                device=entity._device,
                coordinator=entity.coordinator,
                reason="expiry",
                is_device_on=lambda: True,
                allow_away_handling=False,
            )
        )
        await asyncio.wait_for(cleanup_queued.wait(), timeout=2)
        assert not cleanup.done()
    finally:
        release_write.set()

    with pytest.raises(failure):
        await asyncio.wait_for(manual, timeout=2)
    assert cleanup is not None
    await asyncio.wait_for(cleanup, timeout=2)
    api.put_device_fields.assert_awaited_once_with(
        "unit-1",
        {"device": {"scenary": "occupied" if preset == "home" else "vacant"}},
        retry=False,
    )
    assert bucket["sleep_exit_pending"]["unit-1"] == "unverified"
    api.async_set_scenary.assert_not_awaited()
    assert manager.request_recovery.called


async def test_known_unsent_first_step_does_not_mark_ambiguous(
    hass, dummy_coordinator_factory, monkeypatch
):
    """Local rate-limit rejection proves the first PUT was never sent."""
    from custom_components.airzoneclouddaikin.airzone_api import (
        CommandNotSentCooldown,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    _arm_current_sleep_off_evidence(hass)
    manager = Mock()
    bucket["sync_manager"] = manager
    api.put_device_fields.side_effect = CommandNotSentCooldown(hass.loop.time() + 300)
    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_preset_mode("sleep")
    assert bucket.get("sleep_exit_pending", {}).get("unit-1") is None
    manager.request_recovery.assert_not_called()


async def test_expired_sleep_rearm_does_not_override_newer_queued_manual_scene(
    hass, dummy_coordinator_factory, monkeypatch
):
    """A newer scene revision wins while the old request waits for its lock."""
    import asyncio

    from custom_components.airzoneclouddaikin.helpers import (
        acquire_device_lock,
        optimistic_set,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    _arm_current_sleep_off_evidence(hass)
    lock = acquire_device_lock(hass, "climate-entry", "unit-1")
    await lock.acquire()
    try:
        request = asyncio.create_task(entity.async_set_preset_mode("sleep"))
        await asyncio.sleep(0)
        optimistic_set(hass, "climate-entry", "unit-1", "scenary", "vacant")
    finally:
        lock.release()
    await request
    api.put_device_fields.assert_not_awaited()


@pytest.mark.parametrize(
    ("mode", "setpoint", "fan"),
    [("1", "P7", "P3"), ("2", "P8", "P4")],
)
async def test_active_sleep_setpoint_and_fan_preserve_scene(
    hass, dummy_coordinator_factory, monkeypatch, mode, setpoint, fan
):
    """Changing temperature or fan during an active Sleep sends no scene PUT."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode=mode,
        scenary="sleep",
        power="1",
    )
    await entity.async_set_temperature(temperature=23)
    await entity.async_set_fan_mode("high")
    assert [x.args[0]["event"]["option"] for x in api.send_event.await_args_list] == [
        setpoint,
        fan,
    ]
    api.put_device_fields.assert_not_awaited()


async def test_active_sleep_hvac_mode_change_preserves_scene(
    hass, dummy_coordinator_factory, monkeypatch
):
    """The actual ON/Sleep guard must not clear an active session."""
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="2",
        scenary="sleep",
        power="1",
    )
    monkeypatch.setattr(
        climate_module,
        "async_auto_exit_sleep_if_needed",
        async_auto_exit_sleep_if_needed,
    )
    await entity.async_set_hvac_mode(HVACMode.COOL)
    api.put_device_fields.assert_not_awaited()
    assert [x.args[0]["event"]["option"] for x in api.send_event.await_args_list] == [
        "P2"
    ]
    assert entity.preset_mode == "sleep"


async def test_invalid_preset_is_noop(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown preset cannot emit backend writes."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    await entity.async_set_preset_mode("not-a-preset")
    api.put_device_fields.assert_not_awaited()


@pytest.mark.parametrize(
    ("reported_power", "previous_overlay", "method", "command"),
    [
        ("0", "1", "async_turn_off", 0),
        ("1", "0", "async_turn_on", 1),
    ],
)
async def test_explicit_climate_power_intent_overrides_stale_backend_snapshot(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    reported_power: str,
    previous_overlay: str,
    method: str,
    command: int,
) -> None:
    """Latest explicit P1 intent wins while older GET still disagrees."""
    from custom_components.airzoneclouddaikin.helpers import optimistic_set

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power=reported_power
    )
    optimistic_set(hass, "climate-entry", "unit-1", "power", previous_overlay)
    await getattr(entity, method)()
    events = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert len(events) == 1
    assert events[0]["option"] == "P1"
    assert events[0]["value"] == command
    assert entity._overlay_value("power", reported_power) == str(command)


@pytest.mark.parametrize(
    ("method", "initial_power", "value"),
    [("async_turn_on", "0", 1), ("async_turn_off", "1", 0)],
)
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_ambiguous_climate_p1_requests_readback_without_overlay(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    initial_power: str,
    value: int,
    failure: type[BaseException],
) -> None:
    """An uncertain power write is never silently treated as OFF/ON success."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power=initial_power
    )

    async def ambiguous(_payload, *, on_uncertain, **_kwargs):
        on_uncertain()
        raise failure("unconfirmed P1")

    api.send_event.side_effect = ambiguous
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(failure):
        await getattr(entity, method)()

    events = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [(event["option"], event["value"]) for event in events] == [("P1", value)]
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    assert entity._overlay_value("power", initial_power) == initial_power
    entity.async_write_ha_state.assert_not_called()
    climate_module.schedule_post_write_refresh.assert_not_called()


async def test_uncertain_implicit_hvac_p1_requests_refresh_without_p2_replay(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode selection's failed P1 must request recovery before reaching P2."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
        climate_module.schedule_post_write_refresh,
    )

    async def ambiguous(_payload, *, on_uncertain, **_kwargs):
        on_uncertain()
        raise TimeoutError("P1 outcome unknown")

    api.send_event.side_effect = ambiguous

    with pytest.raises(TimeoutError):
        await entity.async_set_hvac_mode(HVACMode.HEAT)

    events = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [(event["option"], event["value"]) for event in events] == [("P1", 1)]
    assert entity.hvac_mode == HVACMode.OFF
    climate_module.schedule_post_write_refresh.assert_called_once_with(
        hass, entity.coordinator, entry_id="climate-entry"
    )


async def test_known_unsent_p1_does_not_request_readback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cooldown rejected before I/O is not an ambiguous accepted write."""
    from custom_components.airzoneclouddaikin.airzone_api import (
        CommandNotSentCooldown,
    )

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="1")
    api.send_event.side_effect = CommandNotSentCooldown(hass.loop.time() + 60)
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(CommandNotSentCooldown):
        await entity.async_turn_off()

    manager.request_recovery.assert_not_called()
    climate_module.schedule_post_write_refresh.assert_not_called()


@pytest.mark.parametrize(
    ("initial", "first", "second", "expected", "final"),
    [
        ("1", "async_turn_off", "async_turn_on", [0, 1], "1"),
        ("0", "async_turn_on", "async_turn_off", [1, 0], "0"),
        ("1", "async_turn_off", "async_turn_off", [0], "0"),
        ("0", "async_turn_on", "async_turn_on", [1], "1"),
    ],
)
async def test_climate_concurrent_power_intents_are_serialized(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    initial: str,
    first: str,
    second: str,
    expected: list[int],
    final: str,
) -> None:
    """A queued P1 request rechecks power after the preceding command finishes."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power=initial)
    entered = asyncio.Event()
    release = asyncio.Event()
    commands: list[int] = []

    async def paused_send(payload: dict[str, Any], **_kwargs) -> None:
        event = payload["event"]
        assert event["option"] == "P1"
        commands.append(event["value"])
        if len(commands) == 1:
            entered.set()
            await release.wait()

    api.send_event.side_effect = paused_send
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
    assert entity._overlay_value("power", initial) == final


async def test_climate_newer_off_supersedes_on_during_scene_preparation(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later OFF still wins if an older ON waits outside the device lock."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    started = asyncio.Event()
    release = asyncio.Event()

    async def paused_preparation(_reason: str, **_kwargs) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(
        entity, "_ensure_occupied_before_active_action", paused_preparation
    )
    old_on = asyncio.create_task(entity.async_turn_on())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await entity.async_turn_off()
    finally:
        release.set()
    await asyncio.wait_for(old_on, timeout=3)

    api.send_event.assert_not_awaited()
    assert entity._overlay_value("power", "0") == "0"


async def test_climate_wake_preparation_does_not_hold_power_lock(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sleep/Away preparation can use the shared lock without deadlocking ON."""
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    lock = acquire_device_lock(hass, "climate-entry", "unit-1")
    prepared: list[bool] = []

    async def prepare(_reason: str, **_kwargs) -> None:
        async with lock:
            prepared.append(True)

    monkeypatch.setattr(entity, "_ensure_occupied_before_active_action", prepare)
    await asyncio.wait_for(entity.async_turn_on(), timeout=3)

    assert prepared == [True]
    assert api.send_event.await_count == 1


async def test_climate_and_switch_fallback_share_power_intent_order(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OFF in Climate followed by a fallback ON must preserve the last ON."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="1")
    switch = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.switch.schedule_post_write_refresh",
        Mock(),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    values: list[int] = []

    async def paused_send(payload: dict[str, Any], **_kwargs) -> None:
        values.append(payload["event"]["value"])
        if len(values) == 1:
            entered.set()
            await release.wait()

    api.send_event.side_effect = paused_send
    climate_off = asyncio.create_task(entity.async_turn_off())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        switch_on = asyncio.create_task(switch._fallback_turn_on())
        await asyncio.sleep(0)
        assert not switch_on.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(climate_off, switch_on), timeout=3)

    assert values == [0, 1]
    assert entity._device_power_on()
    assert switch.is_on


@pytest.mark.parametrize("next_request", ["turn_off", "hvac_off"])
async def test_hvac_active_mode_is_fenced_by_newer_turn_off_during_preparation(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    next_request: str,
) -> None:
    """A delayed mode selection cannot power on after a newer explicit OFF."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_scene(_reason: str, **_kwargs) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(entity, "_ensure_occupied_before_active_action", wait_for_scene)
    pending_mode = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if next_request == "hvac_off":
            await entity.async_set_hvac_mode(HVACMode.OFF)
        else:
            await entity.async_turn_off()
    finally:
        release.set()
    await asyncio.wait_for(pending_mode, timeout=3)

    events = [c.args[0]["event"] for c in api.send_event.await_args_list]
    assert [(event["option"], event["value"]) for event in events] == []
    assert entity._overlay_value("power", "0") == "0"


@pytest.mark.parametrize("first", ["climate", "switch"])
async def test_hvac_off_supersedes_delayed_climate_or_switch_wake(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """HVAC OFF and switch fallback share the generation across entry/device."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    climate, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    switch = AirzonePowerSwitch(climate.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_scene(_reason: str, **_kwargs) -> None:
        entered.set()
        await release.wait()

    if first == "switch":
        monkeypatch.setattr(
            switch, "_ensure_occupied_before_active_action", wait_for_scene
        )
        older = switch._fallback_turn_on
    else:
        monkeypatch.setattr(
            climate, "_ensure_occupied_before_active_action", wait_for_scene
        )
        older = climate.async_turn_on

    pending_wake = asyncio.create_task(older())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await climate.async_set_hvac_mode(HVACMode.OFF)
    finally:
        release.set()
    await asyncio.wait_for(pending_wake, timeout=3)

    events = [c.args[0]["event"] for c in api.send_event.await_args_list]
    assert [(e["option"], e["value"]) for e in events] == []


@pytest.mark.parametrize(
    "order",
    ["turn_off_then_active_mode", "hvac_off_then_active_mode", "active_mode_then_off"],
)
async def test_hvac_mode_and_power_commands_serialize_opposite_intents(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    """Mode activation cannot lose an ON behind a stale GET or a later OFF."""
    initial = "0" if order == "active_mode_then_off" else "1"
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power=initial)
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple[str, Any]] = []

    async def paused_p1(payload: dict[str, Any], **_kwargs) -> None:
        event = payload["event"]
        calls.append((event["option"], event["value"]))
        if len(calls) == 1:
            entered.set()
            await release.wait()

    api.send_event.side_effect = paused_p1
    if order == "turn_off_then_active_mode":
        first_request = entity.async_turn_off()
    elif order == "hvac_off_then_active_mode":
        first_request = entity.async_set_hvac_mode(HVACMode.OFF)
    else:
        first_request = entity.async_set_hvac_mode(HVACMode.HEAT)

    first_task = asyncio.create_task(first_request)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_request = entity.async_set_hvac_mode(
            HVACMode.OFF if order == "active_mode_then_off" else HVACMode.HEAT
        )
        second_task = asyncio.create_task(second_request)
        await asyncio.sleep(0)
        assert not second_task.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=3)

    if order != "active_mode_then_off":
        assert calls == [("P1", 0), ("P1", 1), ("P2", "2")]
        assert entity._overlay_value("power", initial) == "1"
    else:
        assert calls == [("P1", 1), ("P1", 0)]
        assert entity._overlay_value("power", initial) == "0"


@pytest.mark.parametrize("initial_power", ["1", "0"])
async def test_hvac_off_avoids_duplicate_p1_after_pending_power_off(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    initial_power: str,
) -> None:
    """A queued HVAC OFF must see a prior accepted OFF under the same lock."""
    from custom_components.airzoneclouddaikin.helpers import optimistic_set

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power=initial_power
    )
    optimistic_set(hass, "climate-entry", "unit-1", "mode", "2")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def blocked_send(payload: dict[str, Any], **_kwargs) -> None:
        assert payload["event"]["option"] == "P1"
        first_entered.set()
        await release_first.wait()

    api.send_event.side_effect = blocked_send
    if initial_power == "1":
        first_task = asyncio.create_task(entity.async_turn_off())
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=2)
            next_task = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.OFF))
            await asyncio.sleep(0)
            assert not next_task.done()
        finally:
            release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, next_task), timeout=3)
        assert api.send_event.await_count == 1
    else:
        await entity.async_set_hvac_mode(HVACMode.OFF)
        api.send_event.assert_not_awaited()

    assert entity._overlay_value("power", initial_power) == "0"
    device_overlays = hass.data[DOMAIN]["climate-entry"]["optimistic"].get("unit-1", {})
    assert "mode" not in device_overlays


async def test_hvac_mode_avoids_duplicate_power_on_after_optimistic_wake(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale raw OFF must not cause a second P1 after accepted optimistic ON."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    await entity.async_turn_on()
    await entity.async_set_hvac_mode(HVACMode.HEAT)
    commands = [c.args[0]["event"] for c in api.send_event.await_args_list]
    assert [(e["option"], e["value"]) for e in commands] == [
        ("P1", 1),
        ("P2", "2"),
    ]


async def test_reselecting_active_mode_explicitly_exits_away_without_redundant_p2(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active command explicitly clears Away even if P2 already matches.

    Documented integration policy: a user selecting a supported active mode
    leaves the vacant scene before any needed P1/P2 commands. The scene exit
    remains intentional when the requested mode is already active.
    """
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="1",
        scenary="vacant",
    )
    monkeypatch.setattr(
        climate_module,
        "async_auto_exit_sleep_if_needed",
        async_auto_exit_sleep_if_needed,
    )

    await entity.async_set_hvac_mode(HVACMode.COOL)

    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": "occupied"}}, on_start=ANY
    )
    api.send_event.assert_not_awaited()
    assert entity.preset_mode == "home"


async def test_turn_on_already_on_exits_away_without_duplicate_p1(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ON in Away must request Home while preserving idempotent P1."""
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="1",
        scenary="vacant",
    )
    monkeypatch.setattr(
        climate_module,
        "async_auto_exit_sleep_if_needed",
        async_auto_exit_sleep_if_needed,
    )

    await entity.async_turn_on()

    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": "occupied"}}, on_start=ANY
    )
    api.send_event.assert_not_awaited()
    assert entity.preset_mode == "home"
    assert entity._device_power_on()


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
        {
            "event": {
                "cgi": "modmaquina",
                "device_id": "unit-1",
                "option": "P1",
                "value": 1,
            }
        },
        on_start=ANY,
        on_uncertain=ANY,
    )


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("with_manager", [False, True])
async def test_partial_hvac_power_on_failure_requests_readback_without_replaying(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
    with_manager: bool,
) -> None:
    """An accepted P1 followed by an ambiguous P2 must trigger fresh readback."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode="1", power="0"
    )
    api.send_event.side_effect = [None, failure("P2 did not confirm")]
    manager = Mock()
    if with_manager:
        hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(failure):
        await entity.async_set_hvac_mode(HVACMode.HEAT)

    commands = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [event["option"] for event in commands] == ["P1", "P2"]
    assert [event["value"] for event in commands] == [1, "2"]
    assert entity._overlay_value("power", entity._device["power"]) == "1"
    assert "mode" not in hass.data[DOMAIN]["climate-entry"]["optimistic"]["unit-1"]
    entity.async_write_ha_state.assert_called_once()
    if with_manager:
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
        climate_module.schedule_post_write_refresh.assert_not_called()
    else:
        climate_module.schedule_post_write_refresh.assert_called_once_with(
            hass, entity.coordinator, entry_id="climate-entry"
        )


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("with_manager", [False, True])
async def test_ambiguous_hvac_p2_only_failure_requests_readback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
    with_manager: bool,
) -> None:
    """P2 timeouts require readback even when no preceding P1 was sent."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode="1", power="1"
    )
    api.send_event.side_effect = failure("P2 may have been accepted")
    manager = Mock()
    if with_manager:
        hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(failure):
        await entity.async_set_hvac_mode(HVACMode.HEAT)

    events = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [(event["option"], event["value"]) for event in events] == [("P2", "2")]
    assert entity.hvac_mode == HVACMode.COOL
    entity.async_write_ha_state.assert_not_called()
    if with_manager:
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
        climate_module.schedule_post_write_refresh.assert_not_called()
    else:
        climate_module.schedule_post_write_refresh.assert_called_once_with(
            hass, entity.coordinator, entry_id="climate-entry"
        )


async def test_unsent_hvac_p2_only_write_does_not_request_recovery(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known-unsent cooldown rejection must not request a pointless readback."""
    from custom_components.airzoneclouddaikin.airzone_api import (
        CommandNotSentCooldown,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode="1", power="1"
    )
    api.send_event.side_effect = CommandNotSentCooldown(hass.loop.time() + 60)
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager

    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_hvac_mode(HVACMode.HEAT)

    api.send_event.assert_awaited_once()
    manager.request_recovery.assert_not_called()
    climate_module.schedule_post_write_refresh.assert_not_called()


@pytest.mark.parametrize(
    ("modes", "heat_cool_opt_in"),
    [("11101", True), ("11111", False), ("", False)],
)
async def test_unsupported_auto_request_never_powers_on_or_exits_away(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    modes: str,
    heat_cool_opt_in: bool,
) -> None:
    """A forced P2=4 request does not trigger P1 or a scenary transition."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="0",
        scenary="vacant",
        modes=modes,
        heat_cool_opt_in=heat_cool_opt_in,
    )

    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)

    api.send_event.assert_not_awaited()
    api.put_device_fields.assert_not_awaited()
    climate_module.async_auto_exit_sleep_if_needed.assert_not_awaited()


async def test_reported_unapproved_auto_disables_setpoint_and_fan_writes(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced service call cannot write through unapproved reported P2=4."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="4",
        power="1",
        scenary="vacant",
        modes="11101000",
        heat_cool_opt_in=False,
    )

    await entity.async_set_temperature(temperature=23)
    await entity.async_set_fan_mode("high")

    api.send_event.assert_not_awaited()
    api.put_device_fields.assert_not_awaited()
    climate_module.async_auto_exit_sleep_if_needed.assert_not_awaited()


@pytest.mark.parametrize(
    ("modes", "expected_code"),
    [
        ("00100000", "3"),  # FAN_ONLY via P2=3
        ("00100001", "3"),  # ignore P2=8 even if advertised
    ],
)
async def test_fan_only_from_off_uses_supported_code_and_only_then_powers_on(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    modes: str,
    expected_code: str,
) -> None:
    """Selected FAN_ONLY code is supported; power-on precedes the P2 write."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, mode="1", power="0", modes=modes
    )

    await entity.async_set_hvac_mode(HVACMode.FAN_ONLY)

    commands = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [event["option"] for event in commands] == ["P1", "P2"]
    assert [event["value"] for event in commands] == [1, expected_code]


@pytest.mark.parametrize("modes", ["11000000", "00000001", ""])
async def test_unsupported_fan_only_rejects_before_power_or_away_exit(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    modes: str,
) -> None:
    """No unsupported HVAC mode may power on the equipment or exit its preset."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="0",
        scenary="vacant",
        modes=modes,
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await entity.async_set_hvac_mode(HVACMode.FAN_ONLY)

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "fan_only_unsupported"
    api.send_event.assert_not_awaited()
    api.put_device_fields.assert_not_awaited()
    climate_module.async_auto_exit_sleep_if_needed.assert_not_awaited()


async def test_ventilation_selection_normalizes_reported_eight_to_three(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deliberate ventilation selection writes P2=3 even if state reports 8."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="8",
        power="1",
        modes="11101000",
    )
    await entity.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.send_event.assert_awaited_once_with(
        {
            "event": {
                "cgi": "modmaquina",
                "device_id": "unit-1",
                "option": "P2",
                "value": "3",
            }
        },
        on_start=ANY,
    )


@pytest.mark.parametrize(
    ("requested_mode", "bitmask"),
    [
        (HVACMode.COOL, "01101000"),  # no P2=1
        (HVACMode.HEAT, "10101000"),  # no P2=2
        (HVACMode.DRY, "11100000"),  # no P2=5
        (HVACMode.COOL, ""),  # unknown capabilities
        (HVACMode.HEAT, "invalid"),  # malformed capabilities
        (HVACMode.DRY, "111"),  # truncated capabilities
    ],
)
@pytest.mark.parametrize("power", ["0", "1"])
async def test_unadvertised_mode_never_writes_or_exits_away(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    requested_mode: HVACMode,
    bitmask: str,
    power: str,
) -> None:
    """Forced services cannot select an unadvertised P2 or alter device state."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="8",
        power=power,
        scenary="vacant",
        modes=bitmask,
    )

    await entity.async_set_hvac_mode(requested_mode)

    api.send_event.assert_not_awaited()
    api.put_device_fields.assert_not_awaited()
    climate_module.async_auto_exit_sleep_if_needed.assert_not_awaited()


@pytest.mark.parametrize(
    ("requested_mode", "bitmask", "expected_code"),
    [
        (HVACMode.COOL, "10000000", "1"),
        (HVACMode.HEAT, "01000000", "2"),
        (HVACMode.DRY, "00001000", "5"),
    ],
)
async def test_advertised_mode_still_powers_on_then_selects_p2(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    requested_mode: HVACMode,
    bitmask: str,
    expected_code: str,
) -> None:
    """Capability guards preserve ordinary mode changes on supported devices."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="8",
        power="0",
        modes=bitmask,
    )

    await entity.async_set_hvac_mode(requested_mode)

    commands = [call.args[0]["event"] for call in api.send_event.await_args_list]
    assert [event["option"] for event in commands] == ["P1", "P2"]
    assert [event["value"] for event in commands] == [1, expected_code]


async def test_already_in_mode_three_does_not_repeat_p2_write(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing 3-only ventilation remains unchanged with no needless writes."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="3",
        power="1",
        modes="11101000",
    )
    await entity.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.send_event.assert_not_awaited()


@pytest.mark.parametrize("lifecycle", ["unload", "failed_unload", "replacement"])
async def test_partial_mode_command_stays_with_its_runtime(
    hass, dummy_coordinator_factory, monkeypatch, lifecycle
) -> None:
    """Cancel real API work on unload or fail P2 after the runtime changes."""
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.airzoneclouddaikin import async_unload_entry
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI

    entity, _ = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entity._entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    api = AirzoneAPI("test", Mock(), token="synthetic")
    old_manager = Mock(close=AsyncMock())
    bucket.update(api=api, coordinator=entity.coordinator, sync_manager=old_manager)
    entity.coordinator.api = api
    successor = {"coordinator": Mock(), "sync_manager": Mock()}
    blocked = asyncio.Event()
    release = asyncio.Event()
    commands = []

    async def transport(*args, **kwargs):
        command = kwargs["json"]["event"]
        commands.append((command["option"], command["value"]))
        if command["option"] == "P2":
            blocked.set()
            await release.wait()
            raise TimeoutError("Synthetic P2 result unknown")

    monkeypatch.setattr(api, "_authed_request_impl", transport)
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=lifecycle == "unload"),
    )
    task = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
    try:
        async with asyncio.timeout(5):
            await blocked.wait()
            if lifecycle == "replacement":
                hass.data[DOMAIN][entry.entry_id] = successor
            else:
                assert (await async_unload_entry(hass, entry)) is (
                    lifecycle == "unload"
                )
            release.set()
            with pytest.raises(
                asyncio.CancelledError if lifecycle == "unload" else TimeoutError
            ):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert commands == [("P1", 1), ("P2", "2")]
    assert not api._pending
    climate_module.schedule_post_write_refresh.assert_not_called()
    successor["sync_manager"].request_recovery.assert_not_called()
    assert set(successor) == {"coordinator", "sync_manager"}
    if lifecycle == "failed_unload":
        assert hass.data[DOMAIN][entry.entry_id] is bucket
        assert bucket["optimistic"]["unit-1"]["power"]["value"] == "1"
        entity.async_write_ha_state.assert_called_once()
        old_manager.request_recovery.assert_called_once_with(
            "unit-1", require_fresh=True
        )
    else:
        entity.async_write_ha_state.assert_not_called()
        old_manager.request_recovery.assert_not_called()
        if lifecycle == "unload":
            assert entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.parametrize("command", ["climate_p1", "climate_p2", "switch"])
async def test_late_accepted_command_cannot_write_into_successor(
    hass, dummy_coordinator_factory, monkeypatch, command
) -> None:
    """A successful late response cannot send another command or publish."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    climate, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        power="1" if command == "climate_p2" else "0",
    )
    bucket = hass.data[DOMAIN][climate._entry_id]
    bucket["coordinator"] = climate.coordinator
    successor = {"coordinator": Mock(), "sync_manager": Mock()}
    started = asyncio.Event()
    release = asyncio.Event()

    async def transport(payload, **_kwargs):
        started.set()
        await release.wait()

    api.send_event.side_effect = transport
    if command == "switch":
        entity = AirzonePowerSwitch(climate.coordinator, climate._entry_id, "unit-1")
        entity.hass = hass
        monkeypatch.setattr(entity, "async_write_ha_state", Mock())
        operation = entity.async_turn_on()
    else:
        entity = climate
        operation = entity.async_set_hvac_mode(HVACMode.HEAT)
    task = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(5):
            await started.wait()
            hass.data[DOMAIN][climate._entry_id] = successor
            release.set()
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    api.send_event.assert_awaited_once()
    assert api.send_event.await_args.args[0]["event"]["option"] == (
        "P2" if command == "climate_p2" else "P1"
    )
    entity.async_write_ha_state.assert_not_called()
    assert set(successor) == {"coordinator", "sync_manager"}
    successor["sync_manager"].request_recovery.assert_not_called()


@pytest.mark.parametrize("initial", [0, 1])
@pytest.mark.parametrize(
    ("first_platform", "last_platform"),
    [
        ("climate", "climate"),
        ("switch", "switch"),
        ("climate", "switch"),
        ("switch", "climate"),
        ("delegated", "delegated"),
    ],
)
async def test_latest_power_survives_ambiguous_p1(
    hass, dummy_coordinator_factory, monkeypatch, initial, first_platform, last_platform
):
    """An uncertain P1 cannot make the user's opposite request look redundant."""
    from homeassistant.helpers.service import entity_service_call

    from custom_components.airzoneclouddaikin.helpers import (
        acquire_device_lock,
        optimistic_set,
    )
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power=str(initial)
    )
    entity.entity_id = "climate.unit"
    switch = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.switch.schedule_post_write_refresh",
        Mock(),
    )
    monkeypatch.setattr(switch, "_resolve_climate_entity_id", lambda: "climate.unit")
    optimistic_set(hass, "climate-entry", "unit-1", "power", str(initial))
    manager = Mock()
    bucket = hass.data[DOMAIN]["climate-entry"]
    bucket["sync_manager"] = manager
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = acquire_device_lock(hass, "climate-entry", "unit-1")
    acquire = lock.acquire

    async def signal_when_queued():
        if lock.locked():
            queued.set()
        return await acquire()

    monkeypatch.setattr(lock, "acquire", signal_when_queued)
    commands = []

    async def send(payload, **kwargs):
        commands.append(payload["event"]["value"])
        if len(commands) == 1:
            entered.set()
            await release.wait()
            if on_uncertain := kwargs.get("on_uncertain"):
                on_uncertain()
            raise TimeoutError("P1 response lost")

    api.send_event.side_effect = send

    async def on(call):
        await entity_service_call(
            hass, {entity.entity_id: entity}, "async_turn_on", call
        )

    async def off(call):
        await entity_service_call(
            hass, {entity.entity_id: entity}, "async_turn_off", call
        )

    hass.services.async_register("climate", "turn_on", on)
    hass.services.async_register("climate", "turn_off", off)

    def command(platform, value):
        if platform == "delegated":
            hass.states.async_set("climate.unit", "off")
        target = entity if platform == "climate" else switch
        return target.async_turn_on() if value else target.async_turn_off()

    tasks = []
    try:
        async with asyncio.timeout(3):
            tasks.append(asyncio.create_task(command(first_platform, 1 - initial)))
            await entered.wait()
            tasks.append(asyncio.create_task(command(last_platform, initial)))
            await queued.wait()
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert isinstance(results[0], TimeoutError)
    assert results[1] is None
    assert commands == [1 - initial, initial]
    assert entity._overlay_value("power", "?") == str(initial)
    assert not bucket.get("uncertain_power")
    assert (
        entity.async_write_ha_state.call_count + switch.async_write_ha_state.call_count
        == 1
    )
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)


@pytest.mark.parametrize("replace_entry", [False, True])
async def test_delayed_switch_delegation_keeps_original_intent_order(
    hass, dummy_coordinator_factory, monkeypatch, replace_entry
):
    """Entering the delegated Climate handler late cannot reorder user intent."""
    from homeassistant.helpers.service import entity_service_call

    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entity.entity_id = "climate.unit"
    switch = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "_resolve_climate_entity_id", lambda: "climate.unit")
    hass.states.async_set("climate.unit", "off")
    entered, release = asyncio.Event(), asyncio.Event()

    async def on(call):
        entered.set()
        await release.wait()
        await entity_service_call(
            hass, {entity.entity_id: entity}, "async_turn_on", call
        )

    hass.services.async_register("climate", "turn_on", on)
    async with asyncio.timeout(3):
        old = asyncio.create_task(switch.async_turn_on())
        await entered.wait()
        if replace_entry:
            hass.data[DOMAIN].pop("climate-entry")
            entity, successor_api = _climate(
                hass, dummy_coordinator_factory, monkeypatch, power="0"
            )
            entity.entity_id = "climate.unit"
        else:
            await entity.async_turn_off()
        release.set()
        await old
    api.send_event.assert_not_awaited()
    if replace_entry:
        successor_api.send_event.assert_not_awaited()


@pytest.mark.parametrize("lifecycle", ["unload", "failed_unload", "replacement"])
async def test_uncertain_p1_is_owned_by_its_original_entry(
    hass, dummy_coordinator_factory, monkeypatch, lifecycle
):
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.airzoneclouddaikin import async_unload_entry
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI

    entity, _ = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entity._entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    api = AirzoneAPI("synthetic", Mock())
    monkeypatch.setattr(api, "_dispatch_traced", True)
    manager = Mock(close=AsyncMock())
    bucket.update(api=api, sync_manager=manager)
    entity.coordinator.api = api
    old_marker, new_marker = object(), object()
    bucket["uncertain_power"] = {"unit-1": old_marker}
    successor = {
        "coordinator": Mock(),
        "sync_manager": Mock(),
        "uncertain_power": {"unit-1": new_marker},
    }
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def transport(_method, _path, *, on_dispatch, on_uncertain, json, **_kwargs):
        on_dispatch()
        calls.append(json["event"]["value"])
        entered.set()
        try:
            await release.wait()
        finally:
            on_uncertain()
        raise TimeoutError

    monkeypatch.setattr(api, "_request", transport)
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=lifecycle == "unload"),
    )
    task = asyncio.create_task(entity.async_turn_on())
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            if lifecycle == "replacement":
                hass.data[DOMAIN][entry.entry_id] = successor
            else:
                assert (await async_unload_entry(hass, entry)) == (
                    lifecycle == "unload"
                )
            release.set()
            with pytest.raises(
                asyncio.CancelledError if lifecycle == "unload" else TimeoutError
            ):
                await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert calls == [1]
    assert not api._pending
    entity.async_write_ha_state.assert_not_called()
    successor["sync_manager"].request_recovery.assert_not_called()
    assert successor["uncertain_power"] == {"unit-1": new_marker}
    if lifecycle == "failed_unload":
        assert bucket["uncertain_power"]["unit-1"] is not old_marker
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    else:
        manager.request_recovery.assert_not_called()
        if lifecycle == "unload":
            assert not bucket.get("uncertain_power")
        else:
            assert bucket["uncertain_power"] == {"unit-1": old_marker}


@pytest.mark.parametrize("cancel_last", [False, True])
async def test_queued_intermediate_power_intent_never_dispatches(
    hass, dummy_coordinator_factory, monkeypatch, cancel_last
):
    """Only the current queued request may send; lock cancellation registers no P1."""
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    lock = acquire_device_lock(hass, entity._entry_id, "unit-1")
    acquire = lock.acquire
    queued_first, queued_middle, queued_latest = (asyncio.Event() for _ in range(3))
    arrivals = iter((queued_first, queued_middle, queued_latest))

    async def signal_when_queued():
        if lock.locked():
            next(arrivals).set()
        return await acquire()

    monkeypatch.setattr(lock, "acquire", signal_when_queued)
    manager = Mock()
    bucket = hass.data[DOMAIN][entity._entry_id]
    bucket["sync_manager"] = manager
    async with asyncio.timeout(3):
        async with lock:
            first = asyncio.create_task(entity.async_turn_on())
            await queued_first.wait()
            intermediate = asyncio.create_task(entity.async_turn_off())
            await queued_middle.wait()
            latest = asyncio.create_task(entity.async_turn_on())
            await queued_latest.wait()
            if cancel_last:
                latest.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await latest
        await asyncio.gather(first, intermediate)
        if not cancel_last:
            await latest
    assert [
        call.args[0]["event"]["value"] for call in api.send_event.await_args_list
    ] == ([] if cancel_last else [1])
    manager.request_recovery.assert_not_called()
    assert not bucket.get("uncertain_power")


async def test_newer_off_supersedes_queued_on_during_active_sleep(
    hass, dummy_coordinator_factory, monkeypatch
):
    """New OFF supersedes a queued P1 ON; no occupied scene is sent."""
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        power="0",
        scenary="sleep",
    )
    lock = acquire_device_lock(hass, entity._entry_id, "unit-1")
    await lock.acquire()
    try:
        on = asyncio.create_task(entity.async_turn_on())
        await asyncio.sleep(0)
        off = asyncio.create_task(entity.async_turn_off())
        await asyncio.sleep(0)
    finally:
        lock.release()
    await asyncio.wait_for(asyncio.gather(on, off), 3)
    api.put_device_fields.assert_not_awaited()
    api.send_event.assert_not_awaited()
    assert entity._device["scenary"] == "sleep"


@pytest.mark.parametrize("scene", ["vacant", "vacant_to_sleep"])
async def test_newer_off_stops_wake_scene_before_it_dispatches(
    hass, dummy_coordinator_factory, monkeypatch, scene
):
    """A scene held before HTTP dispatch can be abandoned for a newer OFF."""
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
    from custom_components.airzoneclouddaikin.helpers import (
        async_auto_exit_sleep_if_needed,
    )

    entity, _ = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        power="0",
        scenary="vacant" if scene == "vacant_to_sleep" else scene,
    )
    monkeypatch.setattr(
        climate_module,
        "async_auto_exit_sleep_if_needed",
        async_auto_exit_sleep_if_needed,
    )
    api = AirzoneAPI("synthetic", Mock())
    monkeypatch.setattr(api, "_dispatch_traced", True)
    entity.coordinator.api = api
    entered, release, off_queued, home_queued = (asyncio.Event() for _ in range(4))
    sent = []

    async def transport(method, _path, *, on_dispatch=None, **_kwargs):
        if method == "PUT":
            entered.set()
            await release.wait()
        if on_dispatch is not None:
            on_dispatch()
        sent.append(method)

    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    lock = acquire_device_lock(hass, entity._entry_id, "unit-1")
    acquire = lock.acquire

    async def signal_off():
        if lock.locked():
            (off_queued if entered.is_set() else home_queued).set()
        return await acquire()

    monkeypatch.setattr(lock, "acquire", signal_off)
    monkeypatch.setattr(api, "_request", transport)
    async with asyncio.timeout(3):
        if scene == "vacant_to_sleep":
            async with lock:
                on = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
                await home_queued.wait()
                entity.coordinator.data["unit-1"]["scenary"] = "sleep"
        else:
            on = asyncio.create_task(entity.async_turn_on())
        await entered.wait()
        off = asyncio.create_task(entity.async_turn_off())
        await off_queued.wait()
        release.set()
        await asyncio.gather(on, off)

    assert sent == []
    assert not api._pending
    bucket = hass.data[DOMAIN][entity._entry_id]
    assert not bucket.get("sleep_exit_pending")
    assert not bucket.get("optimistic")
    entity.async_write_ha_state.assert_not_called()
    climate_module.schedule_post_write_refresh.assert_not_called()


async def test_independent_climate_service_cannot_inherit_switch_reservation(
    hass, dummy_coordinator_factory, monkeypatch
):
    """A call_service listener runs in its own task and must reserve its own ON."""
    from homeassistant.const import EVENT_CALL_SERVICE
    from homeassistant.core import Context
    from homeassistant.helpers.service import entity_service_call

    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entity.entity_id = "climate.unit"
    switch = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    origin = Context()
    switch.async_set_context(origin)
    monkeypatch.setattr(switch, "_resolve_climate_entity_id", lambda: "climate.unit")
    hass.states.async_set("climate.unit", "off")
    delegated, finish_delegated, run_listener, listener_done = (
        asyncio.Event() for _ in range(4)
    )

    async def on(call):
        if not delegated.is_set():
            delegated.set()
            await finish_delegated.wait()
        await entity_service_call(
            hass, {entity.entity_id: entity}, "async_turn_on", call
        )

    listener_started = False

    async def listener(event):
        nonlocal listener_started
        if not listener_started and event.context.id == origin.id:
            listener_started = True
            await run_listener.wait()
            await hass.services.async_call(
                "climate",
                "turn_on",
                {"entity_id": "climate.unit"},
                blocking=True,
                context=event.context,
            )
            listener_done.set()

    hass.services.async_register("climate", "turn_on", on)
    unsub = hass.bus.async_listen(EVENT_CALL_SERVICE, listener)
    old_on = asyncio.create_task(switch.async_turn_on())
    try:
        async with asyncio.timeout(3):
            await delegated.wait()
            await entity.async_turn_off()
            run_listener.set()
            await listener_done.wait()
            api.send_event.assert_awaited_once()
            assert api.send_event.await_args.args[0]["event"]["value"] == 1
            finish_delegated.set()
            await old_on
    finally:
        unsub()
        run_listener.set()
        finish_delegated.set()
        old_on.cancel()
        await asyncio.gather(old_on, return_exceptions=True)
    api.send_event.assert_awaited_once()


async def test_superseded_p2_keeps_readback_if_newer_off_never_dispatches(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
):
    """An accepted partial wake still needs its normal managed readback."""
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    manager = Mock()
    hass.data[DOMAIN][entity._entry_id]["sync_manager"] = manager
    started, release, queued = (asyncio.Event() for _ in range(3))
    lock = acquire_device_lock(hass, entity._entry_id, "unit-1")
    acquire = lock.acquire

    async def signal_queued():
        if lock.locked():
            queued.set()
        return await acquire()

    async def send(payload, **_kwargs):
        assert payload["event"]["option"] == "P1"
        started.set()
        await release.wait()

    monkeypatch.setattr(lock, "acquire", signal_queued)
    api.send_event.side_effect = send
    async with asyncio.timeout(3):
        activation = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
        await started.wait()
        off = asyncio.create_task(entity.async_turn_off())
        await queued.wait()
        off.cancel()
        with pytest.raises(asyncio.CancelledError):
            await off
        release.set()
        await activation
    api.send_event.assert_awaited_once()
    entity.async_write_ha_state.assert_called_once()
    assert entity._overlay_value("power", "0") == "1"
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)


@pytest.mark.parametrize("initial", [0, 1])
async def test_latest_power_survives_untraced_http_503(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
    socket_enabled,
    aiohttp_server,
    initial,
):
    """A real response from an untraced P1 cannot suppress the queued inverse."""
    from aiohttp import ClientSession, web

    from custom_components.airzoneclouddaikin import airzone_api
    from custom_components.airzoneclouddaikin.helpers import (
        acquire_device_lock,
        optimistic_set,
    )

    entity, _ = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power=str(initial)
    )
    bucket = hass.data[DOMAIN][entity._entry_id]
    manager = Mock()
    bucket["sync_manager"] = manager
    optimistic_set(hass, entity._entry_id, "unit-1", "power", str(initial))
    received = []
    arrived, release, queued = (asyncio.Event() for _ in range(3))

    async def handler(request):
        event = (await request.json())["event"]
        assert event["option"] == "P1"
        received.append(event["value"])
        if len(received) == 1:
            arrived.set()
            await release.wait()
            return web.Response(status=503)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post(airzone_api.API_EVENTS, handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    lock = acquire_device_lock(hass, entity._entry_id, "unit-1")
    acquire = lock.acquire

    async def signal_when_queued():
        if lock.locked():
            queued.set()
        return await acquire()

    monkeypatch.setattr(lock, "acquire", signal_when_queued)
    keep = entity.async_turn_on if initial else entity.async_turn_off
    invert = entity.async_turn_off if initial else entity.async_turn_on
    async with ClientSession() as session:
        api = entity.coordinator.api = airzone_api.AirzoneAPI(
            "synthetic@example.invalid", session
        )
        tasks = []
        try:
            async with asyncio.timeout(3):
                tasks.append(asyncio.create_task(invert()))
                await arrived.wait()
                tasks.append(asyncio.create_task(keep()))
                await queued.wait()
                release.set()
                failed, latest = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert isinstance(failed, HomeAssistantError)
    assert latest is None
    assert received == [1 - initial, initial]
    assert entity._overlay_value("power", "?") == str(initial)
    assert not bucket.get("uncertain_power")
    assert not api._pending
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)


async def test_accepted_p1_survives_caller_cancellation(
    hass, dummy_coordinator_factory, monkeypatch
):
    """Cancellation after P1 dispatch cannot discard its accepted overlay."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power="0", scenary="occupied"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = Mock()
    bucket["sync_manager"] = manager
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def accepted_p1(_payload, *, on_start=None, **_kwargs):
        assert callable(on_start)
        on_start()
        entered.set()
        await release.wait()
        completed.set()

    api.send_event.side_effect = accepted_p1
    activation = asyncio.create_task(entity.async_turn_on())
    await asyncio.wait_for(entered.wait(), timeout=2)
    activation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activation
    assert entity._overlay_value("power", "0") != "1"
    release.set()
    await asyncio.wait_for(completed.wait(), timeout=2)

    async with asyncio.timeout(2):
        while entity._overlay_value("power", "0") != "1":
            await asyncio.sleep(0)
    assert "unit-1" not in bucket.get("uncertain_power", {})
    manager.request_recovery.assert_any_call("unit-1", require_fresh=True)
    api.send_event.assert_awaited_once()


async def test_cancel_before_p1_dispatch_never_claims_acceptance(
    hass, dummy_coordinator_factory, monkeypatch
):
    """Cancellation before dispatch must not create an optimistic P1."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power="0", scenary="occupied"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = Mock()
    bucket["sync_manager"] = manager
    entered = asyncio.Event()

    async def blocked_before_dispatch(_payload, *, on_start=None, **_kwargs):
        assert callable(on_start)
        entered.set()
        await asyncio.Event().wait()

    api.send_event.side_effect = blocked_before_dispatch
    activation = asyncio.create_task(entity.async_turn_on())
    await asyncio.wait_for(entered.wait(), timeout=2)
    activation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activation
    await asyncio.sleep(0)
    assert entity._overlay_value("power", "0") != "1"
    assert not bucket.get("uncertain_power")
    manager.request_recovery.assert_not_called()
