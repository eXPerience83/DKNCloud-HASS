"""Deterministic DKN climate write contract tests: no real devices or cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock, call

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import custom_components.airzoneclouddaikin.climate as climate_module
from custom_components.airzoneclouddaikin.climate import AirzoneClimate
from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.helpers import async_auto_exit_sleep_if_needed


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
        "unit-1", {"device": {"scenary": scenary}}, on_start=ANY
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
        "unit-1", {"device": {"scenary": expected_scenary}}, on_start=ANY
    )
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    assert "scenary" not in bucket.get("optimistic", {}).get("unit-1", {})
    assert bucket.get("sleep_exit_pending", {}).get("unit-1") == (
        "sleep" if preset == "sleep" else None
    )
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

    async def delayed_put(_device_id: str, _payload: dict[str, Any], **_kwargs) -> None:
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

    async def held_put(_device_id: str, _payload: dict[str, Any], **_kwargs) -> None:
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
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
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
    api.async_set_scenary.assert_awaited_once_with(
        "unit-1", "occupied", retry=False, on_start=ANY, on_in_flight=ANY
    )
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
        "unit-1", {"device": {"scenary": "occupied"}}, retry=False, on_start=ANY
    )
    # An accepted optimistic Home should not trigger a redundant second PUT.
    await entity.async_set_preset_mode("home")
    assert api.put_device_fields.await_count == 1


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


def _arm_current_sleep_off_evidence(hass) -> None:
    """Make a cleanup eligible so these tests exercise the intent/pending fence."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import SleepTracking

    hass.data[DOMAIN]["climate-entry"]["sleep_tracking"] = {
        "unit-1": SleepTracking(
            last_scenary="sleep",
            sleep_started_at_utc=dt_util.utcnow() - timedelta(minutes=6),
            start_known=True,
            expected_minutes=5,
        )
    }

    bucket = hass.data[DOMAIN]["climate-entry"]
    bucket["sleep_tracking"]["unit-1"].off_observed = True


async def test_newer_manual_intent_cancels_queued_automatic_cleanup(
    hass, dummy_coordinator_factory, monkeypatch
):
    """The cleanup must not send Home after a newer preset has queued."""
    from custom_components.airzoneclouddaikin.helpers import (
        acquire_device_lock,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    entity.coordinator.data["unit-1"].update(
        sleep_off_confirmed=True,
    )
    api.async_set_scenary = AsyncMock()
    _arm_current_sleep_off_evidence(hass)
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
            )
        )
        await asyncio.sleep(0)
        manual = asyncio.create_task(entity.async_set_preset_mode("sleep"))
        await asyncio.sleep(0)
    finally:
        lock.release()
    await asyncio.wait_for(asyncio.gather(cleanup, manual), timeout=3)
    api.async_set_scenary.assert_not_awaited()
    assert api.put_device_fields.await_count == 0
    assert entity.preset_mode == "sleep"


@pytest.mark.parametrize("preset", ["home", "away"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_ambiguous_explicit_home_away_blocks_old_sleep_cleanup(
    hass, dummy_coordinator_factory, monkeypatch, preset, failure
):
    """A timed-out or cancelled explicit scene must fence later automatic Home."""

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
        "unit-1", {"device": {"scenary": expected}}, retry=False, on_start=ANY
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
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import (
        SleepTracking,
        _derive_state,
        helpers,
    )

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="1"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    bucket["sleep_tracking"] = {
        "unit-1": SleepTracking(
            last_scenary="sleep",
            sleep_started_at_utc=dt_util.utcnow() - timedelta(minutes=6),
            start_known=True,
            expected_minutes=5,
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
        # One complete applied observation satisfies the active-session gate.
        entity.coordinator.data["unit-1"]["power"] = "0"
        entry = SimpleNamespace(entry_id="climate-entry", options={})
        from custom_components.airzoneclouddaikin import (
            _sleep_get_context as climate_get_context,
        )

        _derive_state(
            hass,
            entry,
            entity.coordinator.data,
            fresh_power_ids={"unit-1"},
            sleep_gets={"unit-1": climate_get_context(bucket)},
            fresh_scenary_ids={"unit-1"},
        )
        assert bucket["sleep_tracking"]["unit-1"].off_observed
        manager.request_recovery.reset_mock()

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
        on_start=ANY,
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
    manager = Mock()
    bucket["sync_manager"] = manager
    api.put_device_fields.side_effect = CommandNotSentCooldown(hass.loop.time() + 300)
    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_preset_mode("home")
    assert bucket.get("sleep_exit_pending", {}).get("unit-1") is None
    manager.request_recovery.assert_not_called()


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
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="2",
        scenary="sleep",
        power="1",
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
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
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


@pytest.mark.parametrize("first", ["climate", "active_mode", "switch"])
@pytest.mark.parametrize("next_request", ["turn_off", "hvac_off"])
async def test_newer_off_supersedes_wake_at_away_dispatch(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    next_request: str,
) -> None:
    """A queued Home for ON loses to OFF at dispatch across all power paths."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    climate, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power="0", scenary="vacant"
    )
    switch = AirzonePowerSwitch(climate.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def put_scene(_device_id, payload, *, on_start, **_kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        on_start()
        dispatched.append((asyncio.current_task(), payload["device"]["scenary"]))

    api.put_device_fields.side_effect = put_scene
    if first == "switch":
        wake = switch._fallback_turn_on()
    elif first == "active_mode":
        wake = climate.async_set_hvac_mode(HVACMode.HEAT)
    else:
        wake = climate.async_turn_on()
    pending_wake = asyncio.create_task(wake)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        off = (
            climate.async_set_hvac_mode(HVACMode.OFF)
            if next_request == "hvac_off"
            else climate.async_turn_off()
        )
        pending_off = asyncio.create_task(off)
        await asyncio.sleep(0)
        assert not pending_off.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(pending_wake, pending_off), timeout=3)

    assert dispatched == [(pending_off, "occupied")]
    api.send_event.assert_not_awaited()
    assert climate.preset_mode == "home"
    assert not climate._device_power_on()


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
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="1",
        scenary="vacant",
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
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="1",
        scenary="vacant",
    )

    await entity.async_turn_on()

    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": "occupied"}}, on_start=ANY
    )
    api.send_event.assert_not_awaited()
    assert entity.preset_mode == "home"
    assert entity._device_power_on()


@pytest.mark.parametrize("action", ["turn_on", "set_hvac_mode"])
@pytest.mark.parametrize(
    ("raw_scene", "preset"), [("occupied", "away"), ("vacant", "home")]
)
async def test_active_command_respects_optimistic_scene_before_readback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    raw_scene: str,
    preset: str,
) -> None:
    """Use the acknowledged preset while the coordinator snapshot is stale."""
    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        mode="1",
        power="1",
        scenary=raw_scene,
    )

    async def accept_scene(_device_id, _payload, *, on_start, **_kwargs):
        on_start()

    api.put_device_fields.side_effect = accept_scene

    await entity.async_set_preset_mode(preset)
    selected_scene = "vacant" if preset == "away" else "occupied"
    api.put_device_fields.assert_awaited_once_with(
        "unit-1", {"device": {"scenary": selected_scene}}, on_start=ANY
    )
    assert entity.preset_mode == preset
    assert entity.coordinator.data["unit-1"]["scenary"] == raw_scene

    if action == "turn_on":
        await entity.async_turn_on()
    else:
        await entity.async_set_hvac_mode(HVACMode.COOL)

    expected = [call("unit-1", {"device": {"scenary": selected_scene}}, on_start=ANY)]
    if preset == "away":
        expected.append(
            call("unit-1", {"device": {"scenary": "occupied"}}, on_start=ANY)
        )
    assert api.put_device_fields.await_args_list == expected
    api.send_event.assert_not_awaited()
    assert entity.preset_mode == "home"
    assert entity._device_power_on()


async def test_reselect_active_mode_waits_for_inflight_away_ack(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode request queued behind Away must see its acknowledged scene."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    async def accept_scene(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        if payload["device"]["scenary"] == "vacant":
            started.set()
            await release.wait()

    api.put_device_fields.side_effect = accept_scene
    away = asyncio.create_task(entity.async_set_preset_mode("away"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        active_mode = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.COOL))
        await asyncio.sleep(0)
        assert hass.data[DOMAIN]["climate-entry"]["power_intents"]["unit-1"] == 1
        assert not active_mode.done()
        assert entity.preset_mode == "home"
    finally:
        release.set()

    await asyncio.wait_for(asyncio.gather(away, active_mode), timeout=3)
    assert api.put_device_fields.await_args_list == [
        call("unit-1", {"device": {"scenary": "vacant"}}, on_start=ANY),
        call("unit-1", {"device": {"scenary": "occupied"}}, on_start=ANY),
    ]
    api.send_event.assert_not_awaited()
    assert entity.preset_mode == "home"


@pytest.mark.parametrize(
    ("action", "target_power"),
    [
        ("climate_off", 0),
        ("hvac_off", 0),
        ("switch_off", 0),
        ("switch_on", 1),
    ],
)
@pytest.mark.parametrize("initial_power", ["0", "1"])
async def test_manual_power_controls_leave_away(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    target_power: int,
    initial_power: str,
) -> None:
    """Every supported manual power path cancels Away, even without a P1."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        power=initial_power,
        scenary="vacant",
    )
    switch = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "_resolve_climate_entity_id", lambda: None)
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
        Mock(),
    )

    async def accept_write(_payload, *, on_start, **_kwargs):
        on_start()

    async def accept_scene(_device_id, _payload, *, on_start, **_kwargs):
        on_start()

    api.send_event.side_effect = accept_write
    api.put_device_fields.side_effect = accept_scene
    if action == "climate_off":
        await entity.async_turn_off()
    elif action == "hvac_off":
        await entity.async_set_hvac_mode(HVACMode.OFF)
    elif action == "switch_off":
        await switch.async_turn_off()
    else:
        await switch.async_turn_on()

    events = [c.args[0]["event"] for c in api.send_event.await_args_list]
    expected = [] if initial_power == str(target_power) else [("P1", target_power)]
    assert [(event["option"], event["value"]) for event in events] == expected
    scenes = [c.args[1] for c in api.put_device_fields.await_args_list]
    assert scenes == [{"device": {"scenary": "occupied"}}]
    assert entity.preset_mode == "home"
    assert entity._device_power_on() is bool(target_power)
    assert switch.is_on is bool(target_power)


@pytest.mark.parametrize("platform", ["climate", "switch"])
@pytest.mark.parametrize("initial_power", ["0", "1"])
async def test_on_rechecks_power_after_away_ack(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    initial_power: str,
) -> None:
    """A remote power update during Home preparation changes P1 deduplication."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    climate, api = _climate(
        hass,
        dummy_coordinator_factory,
        monkeypatch,
        power=initial_power,
        scenary="vacant",
    )
    switch = AirzonePowerSwitch(climate.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    order = []
    updated_power = "0" if initial_power == "1" else "1"

    async def home(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        assert payload == {"device": {"scenary": "occupied"}}
        climate.coordinator.data["unit-1"]["power"] = updated_power
        order.append("home")

    async def power(payload, *, on_start, **_kwargs):
        on_start()
        assert payload["event"]["option"] == "P1"
        assert payload["event"]["value"] == 1
        order.append("p1")

    api.put_device_fields.side_effect = home
    api.send_event.side_effect = power
    climate_module.schedule_post_write_refresh.side_effect = lambda *_args, **_kwargs: (
        order.append("refresh")
    )
    if platform == "climate":
        await climate.async_turn_on()
    else:
        await switch._fallback_turn_on()

    expected = ["home", "refresh"]
    if updated_power == "0":
        expected += ["p1", "refresh"]
    assert order == expected
    assert api.send_event.await_count == int(updated_power == "0")
    assert climate.preset_mode == "home"
    assert switch.is_on


async def test_temperature_clamps_after_away_ack(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote limit update during Home preparation constrains the setpoint."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="vacant"
    )

    async def home(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        assert payload == {"device": {"scenary": "occupied"}}
        entity.coordinator.data["unit-1"]["max_limit_cold"] = 26

    api.put_device_fields.side_effect = home
    await entity.async_set_temperature(temperature=28)

    api.send_event.assert_awaited_once_with(
        {
            "event": {
                "cgi": "modmaquina",
                "device_id": "unit-1",
                "option": "P7",
                "value": "26.0",
            }
        }
    )
    assert entity.preset_mode == "home"
    assert entity.target_temperature == 26


@pytest.mark.parametrize("later_away", [False, True])
@pytest.mark.parametrize("platform", ["climate", "switch"])
async def test_off_takes_priority_over_away_exit(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    later_away: bool,
    platform: str,
) -> None:
    """A failed or superseded Home cannot suppress OFF or claim Home success."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    climate, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power="1", scenary="vacant"
    )
    switch = AirzonePowerSwitch(climate.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager
    power_dispatched, release = asyncio.Event(), asyncio.Event()
    order = []

    async def off(payload, *, on_start, **_kwargs):
        on_start()
        event = payload["event"]
        order.append((event["option"], event["value"]))
        power_dispatched.set()
        await release.wait()

    async def ambiguous_home(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        order.append(("scene", payload["device"]["scenary"]))
        raise TimeoutError("Home acceptance unknown")

    api.send_event.side_effect = off
    api.put_device_fields.side_effect = ambiguous_home
    pending_off = asyncio.create_task(
        switch._fallback_turn_off()
        if platform == "switch"
        else climate.async_turn_off()
    )
    try:
        await asyncio.wait_for(power_dispatched.wait(), timeout=2)
        api.put_device_fields.assert_not_awaited()
        if later_away:
            away = asyncio.create_task(climate.async_set_preset_mode("away"))
            await asyncio.sleep(0)
            assert not away.done()
    finally:
        release.set()
    await asyncio.wait_for(pending_off, timeout=2)
    if later_away:
        await asyncio.wait_for(away, timeout=2)
        assert order == [("P1", 0)]
        api.put_device_fields.assert_not_awaited()
        manager.request_recovery.assert_not_called()
    else:
        assert order == [("P1", 0), ("scene", "occupied")]
        api.put_device_fields.assert_awaited_once()
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    assert climate.preset_mode == "away"
    assert not switch.is_on


@pytest.mark.parametrize("platform", ["climate", "hvac_off", "switch"])
@pytest.mark.parametrize("home_fails", [False, True])
async def test_off_publishes_power_before_away_ack(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    home_fails: bool,
) -> None:
    """Publish acknowledged OFF without waiting for the Home PUT outcome."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    climate, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, power="1", scenary="vacant"
    )
    switch = AirzonePowerSwitch(climate.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    publisher = switch if platform == "switch" else climate
    published = []
    before_home_ack = []

    def publish():
        published.append((climate._device_power_on(), climate.preset_mode))

    publisher.async_write_ha_state.side_effect = publish

    async def off(payload, *, on_start, **_kwargs):
        on_start()

    async def home(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        before_home_ack.extend(published)
        if home_fails:
            raise TimeoutError("Home acceptance unknown")

    api.send_event.side_effect = off
    api.put_device_fields.side_effect = home
    if platform == "switch":
        await switch._fallback_turn_off()
    elif platform == "hvac_off":
        await climate.async_set_hvac_mode(HVACMode.OFF)
    else:
        await climate.async_turn_off()

    assert before_home_ack == [(False, "away")]
    expected = [(False, "away")]
    if not home_fails:
        expected.append((False, "home"))
    assert published == expected
    api.send_event.assert_awaited_once()
    assert api.send_event.await_args.args[0]["event"]["value"] == 0
    api.put_device_fields.assert_awaited_once()
    assert api.put_device_fields.await_args.args[1] == {
        "device": {"scenary": "occupied"}
    }
    assert not switch.is_on


@pytest.mark.parametrize("adjustment", ["temperature", "fan"])
@pytest.mark.parametrize("later_away", [False, True])
async def test_adjustment_uses_away_ack_and_respects_later_preset(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    adjustment: str,
    later_away: bool,
) -> None:
    """A valid adjustment sees an earlier Away ACK and honors a later manual Away."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def put_scene(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        scene = payload["device"]["scenary"]
        order.append(scene)
        if not entered.is_set():
            entered.set()
            await release.wait()

    async def send_adjustment(payload, **_kwargs):
        order.append(payload["event"]["option"])

    api.put_device_fields.side_effect = put_scene
    api.send_event.side_effect = send_adjustment
    first_away = asyncio.create_task(entity.async_set_preset_mode("away"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        adjust = (
            entity.async_set_temperature(temperature=23)
            if adjustment == "temperature"
            else entity.async_set_fan_mode("high")
        )
        control = asyncio.create_task(adjust)
        await asyncio.sleep(0)
        assert not control.done()
        tasks = [first_away, control]
        if later_away:
            tasks.append(asyncio.create_task(entity.async_set_preset_mode("away")))
            await asyncio.sleep(0)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    option = "P7" if adjustment == "temperature" else "P3"
    if later_away:
        assert order == ["vacant", option]
        assert entity.preset_mode == "away"
    else:
        assert order == ["vacant", "occupied", option]
        assert entity.preset_mode == "home"


@pytest.mark.parametrize("home_dispatched", [False, True])
async def test_later_manual_away_wins_against_prepared_home(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    home_dispatched: bool,
) -> None:
    """Manual Away fences an unsent Home or follows an already dispatched Home."""
    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="vacant"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def put_scene(_device_id, payload, *, on_start, **_kwargs):
        scene = payload["device"]["scenary"]
        if scene == "occupied" and not home_dispatched:
            entered.set()
            await release.wait()
        on_start()
        dispatched.append(scene)
        if scene == "occupied" and home_dispatched:
            entered.set()
            await release.wait()

    api.put_device_fields.side_effect = put_scene
    turn_on = asyncio.create_task(entity.async_turn_on())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        away = asyncio.create_task(entity.async_set_preset_mode("away"))
        await asyncio.sleep(0)
        assert not away.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(turn_on, away), timeout=3)

    assert dispatched == (["occupied", "vacant"] if home_dispatched else [])
    assert entity.preset_mode == "away"
    api.send_event.assert_not_awaited()


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
        on_in_flight=ANY,
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


async def test_latest_power_survives_ambiguous_p1(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
):
    """A lost ON response cannot cause the next explicit OFF to be skipped."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    switch = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
    switch.hass = hass
    monkeypatch.setattr(switch, "async_write_ha_state", Mock())
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
        Mock(),
    )
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager
    commands = []

    async def send(payload, **callbacks):
        commands.append(payload["event"]["value"])
        if len(commands) == 1:
            if on_uncertain := callbacks.get("on_uncertain"):
                on_uncertain()
            raise TimeoutError("P1 response lost")

    api.send_event.side_effect = send
    with pytest.raises(TimeoutError, match="P1 response lost"):
        await entity.async_turn_on()
    await switch.async_turn_off()

    assert commands == [1, 0]
    assert entity._overlay_value("power", "?") == "0"
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
    old = asyncio.create_task(switch.async_turn_on())
    try:
        async with asyncio.timeout(3):
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
    finally:
        release.set()
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
    api.send_event.assert_not_awaited()
    if replace_entry:
        successor_api.send_event.assert_not_awaited()


@pytest.mark.parametrize(
    "boundary",
    [
        "connect",
        "writer_cancel",
        "retry_cancel",
        "in_flight",
        "in_flight_429",
        "ack",
        "backoff",
        "unload",
        "failed_unload",
    ],
)
async def test_traced_p1_cancellation_boundaries(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
    socket_enabled,
    aiohttp_server,
    boundary,
):
    """Exercise cancellation against the actual runtime session and HTTP peer."""
    from aiohttp import web
    from aiohttp.client_proto import ResponseHandler
    from aiohttp.http_writer import StreamWriter
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.airzoneclouddaikin import (
        airzone_api,
        async_create_runtime_session,
        async_unload_entry,
    )
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, _ = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entity._entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = Mock(close=AsyncMock())
    lock = acquire_device_lock(hass, entry.entry_id, "unit-1")
    reached, release = asyncio.Event(), asyncio.Event()
    received = []
    written = []
    settled_on = asyncio.Event()

    async def handler(request):
        value = (await request.json())["event"]["value"]
        received.append(value)
        if value == 0:
            assert settled_on.is_set()
            assert entity._overlay_value("power", "?") == "1"
        if len(received) == 1:
            if boundary in {"in_flight", "in_flight_429", "unload", "failed_unload"}:
                reached.set()
                await release.wait()
            if boundary in {"backoff", "in_flight_429", "retry_cancel"}:
                return web.Response(status=429)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post(airzone_api.API_EVENTS, handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    write = StreamWriter._write

    def observe_write(writer, chunk):
        write(writer, chunk)
        if isinstance(writer._protocol, ResponseHandler):
            written.append(bytes(chunk))

    writelines = StreamWriter._writelines

    def observe_writelines(writer, chunks):
        chunks = tuple(chunks)
        writelines(writer, chunks)
        if isinstance(writer._protocol, ResponseHandler):
            written.extend(map(bytes, chunks))

    monkeypatch.setattr(StreamWriter, "_write", observe_write)
    monkeypatch.setattr(StreamWriter, "_writelines", observe_writelines)
    monkeypatch.setattr(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=boundary == "unload"),
    )
    session = async_create_runtime_session(hass)
    api = entity.coordinator.api = airzone_api.AirzoneAPI(
        "synthetic@example.invalid", session
    )
    bucket.update(api=api, sync_manager=manager)
    connect = session.connector.connect
    request = api._request

    async def blocked_connect(*args, **kwargs):
        reached.set()
        await release.wait()
        return await connect(*args, **kwargs)

    async def blocked_backoff(_delay):
        if boundary == "retry_cancel":
            on.cancel()
            reached.set()
        else:
            reached.set()
            await release.wait()
        monkeypatch.setattr(api, "_now", lambda: api._cooldown_until)

    async def cancel_after_ack(*args, **kwargs):
        result = await request(*args, **kwargs)
        on.cancel()
        reached.set()
        return result

    async def track_ack(*args, **kwargs):
        result = await request(*args, **kwargs)
        if kwargs["json"]["event"]["value"] == 1:
            settled_on.set()
        return result

    if boundary == "in_flight_429":
        monkeypatch.setattr(api, "_now", lambda: 0.0)

    write_headers = StreamWriter.write_headers

    async def cancel_at_writer(writer, status_line, headers):
        if status_line.startswith("POST "):
            on.cancel()
            reached.set()
        await write_headers(writer, status_line, headers)

    if boundary == "writer_cancel":
        monkeypatch.setattr(StreamWriter, "write_headers", cancel_at_writer)
    if boundary == "in_flight":
        monkeypatch.setattr(api, "_request", track_ack)
    if boundary == "connect":
        monkeypatch.setattr(session.connector, "connect", blocked_connect)
    elif boundary in {"backoff", "in_flight_429", "retry_cancel"}:
        monkeypatch.setattr(api, "_sleep", blocked_backoff)
    elif boundary == "ack":
        monkeypatch.setattr(api, "_request", cancel_after_ack)

    on = asyncio.create_task(entity.async_turn_on())
    tasks = [on]
    try:
        async with asyncio.timeout(4):
            await reached.wait()
            pending = tuple(api._pending)
            if boundary not in {"ack", "writer_cancel", "retry_cancel"}:
                on.cancel()
                await asyncio.sleep(0)
            if boundary in {"in_flight", "in_flight_429", "unload", "failed_unload"}:
                on.cancel()
                await asyncio.sleep(0)
                retained_lock = lock.locked() and not on.done()
            if boundary == "in_flight":
                off = asyncio.create_task(entity.async_turn_off())
                tasks.append(off)
                await asyncio.sleep(0)
                assert received == [1]
            if boundary in {"unload", "failed_unload"}:
                assert await async_unload_entry(hass, entry) == (boundary == "unload")
            if boundary in {"connect", "backoff"}:
                await asyncio.wait({on})
                uncertainty_before_release = bool(bucket.get("uncertain_power"))
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await on
            await asyncio.gather(*pending, return_exceptions=True)
            if boundary == "in_flight":
                await off
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*tuple(api._pending), return_exceptions=True)

    assert not api._pending
    assert not lock.locked()
    assert not bucket.get("uncertain_power")
    if boundary in {"connect", "backoff"}:
        assert not uncertainty_before_release
        if boundary == "connect":
            assert written == []
            assert received == []
        else:
            assert b"".join(written).count(b"POST /events?") == 1
            assert received == [1]
        manager.request_recovery.assert_not_called()
    elif boundary in {"writer_cancel", "retry_cancel"}:
        if boundary == "writer_cancel":
            assert written == []
            assert received == []
        else:
            assert b"".join(written).count(b"POST /events?") == 1
            assert received == [1]
        manager.request_recovery.assert_not_called()
    elif boundary == "in_flight":
        assert retained_lock
        assert received == [1, 0]
        assert entity._overlay_value("power", "?") == "0"
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
    elif boundary == "in_flight_429":
        assert retained_lock
        assert received == [1]
        assert api._cooldown_until > api._now()
        manager.request_recovery.assert_not_called()
    elif boundary == "unload":
        assert retained_lock
        assert received == [1]
        assert entry.entry_id not in hass.data.get(DOMAIN, {})
        manager.request_recovery.assert_not_called()
    else:
        if boundary == "failed_unload":
            assert retained_lock
        assert received == [1]
        assert entity._overlay_value("power", "?") == "1"
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)


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


async def test_explicit_off_after_p2_timeout_and_power_overlay_expiry(
    hass, dummy_coordinator_factory, monkeypatch
):
    """Cached OFF cannot suppress the explicit OFF after a partial wake."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    bucket = hass.data[DOMAIN][entity._entry_id]
    manager = bucket["sync_manager"] = Mock()  # Record readback without applying it.
    commands = []

    async def send(payload, **_kwargs):
        event = payload["event"]
        commands.append((event["option"], event["value"]))
        if event["option"] == "P2":
            raise TimeoutError("P2 response timed out")

    api.send_event.side_effect = send
    with pytest.raises(TimeoutError, match="P2 response timed out"):
        await entity.async_set_hvac_mode(HVACMode.HEAT)
    assert entity._overlay_value("power", "0") == "1"
    await entity.async_turn_on()  # The accepted ON still deduplicates during its TTL.
    clock = hass.loop.time
    with monkeypatch.context() as patched:
        patched.setattr(hass.loop, "time", lambda: clock() + 31)
        await entity.async_turn_off()

    assert commands == [("P1", 1), ("P2", "2"), ("P1", 0)]
    assert (
        manager.request_recovery.call_args_list.count(
            call("unit-1", require_fresh=True)
        )
        == 1
    )
    assert not bucket.get("uncertain_power")


@pytest.mark.parametrize("observed_sleep", [False, True])
async def test_ambiguous_away_exit_preserves_sleep_entry_readback(
    hass, dummy_coordinator_factory, monkeypatch, observed_sleep
):
    """Only an observed Sleep exit needs the strict Home confirmation fence."""
    from custom_components.airzoneclouddaikin import _derive_state, _sleep_get_context

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="vacant", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = bucket["sync_manager"] = Mock()
    data = entity.coordinator.data
    device = data["unit-1"]
    device["sleep_time"] = 30
    entry = SimpleNamespace(entry_id="climate-entry", options={})
    _derive_state(hass, entry, data)

    async def ambiguous_put(_device_id, payload, *, on_start, **_kwargs):
        on_start()
        if payload["device"]["scenary"] == "occupied" and observed_sleep:
            device["scenary"] = "sleep"
            _derive_state(
                hass, entry, data, fresh_scenary_ids={"unit-1"}, from_push=True
            )
        raise TimeoutError("Scene acceptance unknown")

    api.put_device_fields.side_effect = ambiguous_put
    with pytest.raises(TimeoutError):
        await entity.async_set_preset_mode("sleep")
    pending = bucket["sleep_exit_pending"]
    first_marker = pending["unit-1"]
    assert first_marker == "sleep"
    old_context = _sleep_get_context(bucket)

    await entity.async_turn_off()
    marker = pending["unit-1"]
    assert marker is not first_marker
    assert marker == ("unverified" if observed_sleep else "sleep")
    assert manager.request_recovery.call_args_list == [
        call("unit-1", require_fresh=True),
        call("unit-1", require_fresh=True),
    ]

    device["scenary"] = "vacant"
    _derive_state(hass, entry, data, fresh_scenary_ids={"unit-1"}, from_push=True)
    _derive_state(
        hass,
        entry,
        data,
        fresh_scenary_ids={"unit-1"},
        sleep_gets={"unit-1": old_context},
    )
    assert pending["unit-1"] is marker
    with pytest.raises(HomeAssistantError, match="Confirm"):
        await entity.async_set_preset_mode("sleep")
    assert api.put_device_fields.await_count == 2

    _derive_state(
        hass,
        entry,
        data,
        fresh_scenary_ids={"unit-1"},
        sleep_gets={"unit-1": _sleep_get_context(bucket)},
    )
    if observed_sleep:
        assert pending["unit-1"] is marker
        with pytest.raises(HomeAssistantError, match="Confirm"):
            await entity.async_set_preset_mode("sleep")
        device["scenary"] = "occupied"
        _derive_state(
            hass,
            entry,
            data,
            fresh_scenary_ids={"unit-1"},
            sleep_gets={"unit-1": _sleep_get_context(bucket)},
        )
    assert not pending
    api.put_device_fields.side_effect = None
    await entity.async_set_preset_mode("sleep")
    assert [
        c.args[1]["device"]["scenary"] for c in api.put_device_fields.await_args_list
    ] == ["sleep", "occupied", "sleep"]
    api.send_event.assert_not_awaited()


async def test_sleep_reselection_requires_applied_home_before_new_cycle(
    hass, dummy_coordinator_factory, monkeypatch
):
    from types import SimpleNamespace

    from custom_components.airzoneclouddaikin import _derive_state, _sleep_get_context

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="occupied", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    data = entity.coordinator.data
    data["unit-1"]["sleep_time"] = 30
    entry = SimpleNamespace(entry_id="climate-entry", options={})
    _derive_state(hass, entry, data)
    await entity.async_set_preset_mode("sleep")
    api.put_device_fields.assert_awaited_once()
    old = _sleep_get_context(bucket)
    await entity.async_set_preset_mode("home")
    with pytest.raises(HomeAssistantError, match="Confirm"):
        await entity.async_set_preset_mode("sleep")
    data["unit-1"]["scenary"] = "occupied"
    _derive_state(
        hass, entry, data, fresh_scenary_ids={"unit-1"}, sleep_gets={"unit-1": old}
    )
    with pytest.raises(HomeAssistantError, match="Confirm"):
        await entity.async_set_preset_mode("sleep")
    _derive_state(
        hass,
        entry,
        data,
        fresh_scenary_ids={"unit-1"},
        sleep_gets={"unit-1": _sleep_get_context(bucket)},
    )
    await entity.async_set_preset_mode("sleep")
    assert [
        c.args[1]["device"]["scenary"] for c in api.put_device_fields.await_args_list
    ] == ["sleep", "occupied", "sleep"]
    api.send_event.assert_not_awaited()
    tracking = bucket["sleep_tracking"]["unit-1"]
    end = tracking.end
    assert end is not None
    for action in (entity.async_turn_on, entity.async_turn_off):
        await action()
        assert tracking.end == end
    await entity.async_set_preset_mode("sleep")
    assert tracking.end == end
    assert api.put_device_fields.await_count == 3
    assert [c.args[0]["event"]["value"] for c in api.send_event.await_args_list] == [
        1,
        0,
    ]


async def test_on_confirms_home_before_power(
    hass, dummy_coordinator_factory, monkeypatch
):
    from custom_components.airzoneclouddaikin import _derive_state, _sleep_get_context

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    entry = SimpleNamespace(
        entry_id="climate-entry", options={"sleep_cleanup_enabled": False}
    )
    _arm_current_sleep_off_evidence(hass)
    commands = []

    async def exit_sleep(_device, scene, **callbacks):
        callbacks["on_start"]()
        commands.append(scene)

    async def refresh():
        api.send_event.assert_not_awaited()
        context = _sleep_get_context(bucket)
        entity._device["scenary"] = "occupied"
        _derive_state(
            hass,
            entry,
            entity.coordinator.data,
            fresh_scenary_ids={"unit-1"},
            sleep_gets={"unit-1": context},
        )

    async def send(payload, **callbacks):
        callbacks["on_start"]()
        commands.append(payload["event"]["value"])

    api.async_set_scenary = AsyncMock(side_effect=exit_sleep)
    api.send_event.side_effect = send
    monkeypatch.setattr(entity.coordinator, "async_refresh", refresh, raising=False)
    await entity.async_turn_on()
    assert commands == ["occupied", 1]


async def test_manual_sleep_exit_422_preserves_prior_pending(
    hass, dummy_coordinator_factory, monkeypatch
):
    from aiohttp import ClientResponseError

    from custom_components.airzoneclouddaikin.helpers import SleepExitStatus

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="sleep", power="0"
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    api.put_device_fields.side_effect = ClientResponseError(Mock(), (), status=422)
    with pytest.raises(ClientResponseError):
        await entity.async_set_preset_mode("home")
    pending = bucket["sleep_exit_pending"]
    assert not pending
    previous = SleepExitStatus("unverified")
    pending["unit-1"] = previous
    with pytest.raises(ClientResponseError):
        await entity.async_set_preset_mode("home")
    assert pending["unit-1"] is previous


@pytest.mark.parametrize(
    ("initial_scene", "initial_preset", "other_scene", "other_preset"),
    [
        ("occupied", "home", "vacant", "away"),
        ("vacant", "away", "occupied", "home"),
    ],
)
@pytest.mark.parametrize("outcome", ["ambiguous", "unsent", "acknowledged"])
async def test_explicit_preset_after_ordinary_scene_outcome(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
    initial_scene,
    initial_preset,
    other_scene,
    other_preset,
    outcome,
):
    """A new explicit choice survives ambiguity, but reliable duplicates do not."""
    from custom_components.airzoneclouddaikin.airzone_api import CommandNotSentCooldown

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary=initial_scene
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = bucket["sync_manager"] = Mock()
    server_scene = initial_scene
    dispatched = []

    async def put_scene(_device_id, payload, *, on_start, **_kwargs):
        nonlocal server_scene
        if not dispatched and outcome == "unsent":
            raise CommandNotSentCooldown(hass.loop.time() + 60)
        on_start()
        server_scene = payload["device"]["scenary"]
        dispatched.append(server_scene)
        if len(dispatched) == 1 and outcome == "ambiguous":
            raise TimeoutError("Scene applied but response lost")

    api.put_device_fields.side_effect = put_scene
    if outcome == "ambiguous":
        with pytest.raises(TimeoutError):
            await entity.async_set_preset_mode(other_preset)
        manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)
        assert entity.preset_mode == initial_preset
        assert server_scene == other_scene
    elif outcome == "unsent":
        with pytest.raises(CommandNotSentCooldown):
            await entity.async_set_preset_mode(other_preset)
        manager.request_recovery.assert_not_called()
        assert server_scene == initial_scene
    else:
        await entity.async_set_preset_mode(other_preset)
        assert entity.preset_mode == other_preset

    # Deliberately withhold remote readback until the second explicit selection.
    assert entity.coordinator.data["unit-1"]["scenary"] == initial_scene
    requested = other_preset if outcome == "acknowledged" else initial_preset
    await entity.async_set_preset_mode(requested)

    expected = {
        "ambiguous": [other_scene, initial_scene],
        "unsent": [],
        "acknowledged": [other_scene],
    }
    assert dispatched == expected[outcome]
    assert api.put_device_fields.await_count == (2 if outcome == "ambiguous" else 1)
    assert server_scene == (other_scene if outcome == "acknowledged" else initial_scene)
    assert entity.preset_mode == requested
    assert not bucket.get("sleep_exit_pending", {})
    api.send_event.assert_not_awaited()


@pytest.mark.parametrize("platform", ["climate", "switch"])
@pytest.mark.parametrize("power", ["0", "1"])
async def test_explicit_away_after_ambiguous_manual_home(
    hass, dummy_coordinator_factory, monkeypatch, platform, power
):
    """An explicit Away must survive an ambiguous automatic exit from Away."""
    from custom_components.airzoneclouddaikin.switch import AirzonePowerSwitch

    entity, api = _climate(
        hass, dummy_coordinator_factory, monkeypatch, scenary="vacant", power=power
    )
    bucket = hass.data[DOMAIN]["climate-entry"]
    manager = bucket["sync_manager"] = Mock()
    server_scene = "vacant"
    dispatched = []

    async def put_scene(_device_id, payload, *, on_start, **_kwargs):
        nonlocal server_scene
        on_start()
        server_scene = payload["device"]["scenary"]
        dispatched.append(server_scene)
        if len(dispatched) == 1:
            raise TimeoutError("Home applied but response lost")

    api.put_device_fields.side_effect = put_scene
    target = entity
    if platform == "switch":
        target = AirzonePowerSwitch(entity.coordinator, "climate-entry", "unit-1")
        target.hass = hass
        monkeypatch.setattr(target, "_resolve_climate_entity_id", lambda: None)
        monkeypatch.setattr(target, "async_write_ha_state", Mock())

    # Redundant manual power must not emit P1, but does attempt Home.
    if power == "1":
        await target.async_turn_on()
    else:
        await target.async_turn_off()

    assert dispatched == ["occupied"]
    assert server_scene == "occupied"
    assert entity.preset_mode == "away"
    manager.request_recovery.assert_called_once_with("unit-1", require_fresh=True)

    await entity.async_set_preset_mode("away")

    assert dispatched == ["occupied", "vacant"]
    assert api.put_device_fields.await_count == 2
    assert server_scene == "vacant"
    assert entity.preset_mode == "away"
    assert not bucket.get("sleep_exit_pending", {})
    api.send_event.assert_not_awaited()
