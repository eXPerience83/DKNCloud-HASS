"""Deterministic DKN climate write contract tests: no real devices or cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

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
    entity = AirzoneClimate(coordinator, "climate-entry", "unit-1")
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", Mock())
    monkeypatch.setattr(climate_module, "schedule_post_write_refresh", Mock())
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
        force_exit_requested=True,
    )
    hass.data[DOMAIN]["climate-entry"]["sleep_tracking"] = {"unit-1": tracking}
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
    manager = Mock()
    hass.data[DOMAIN]["climate-entry"]["sync_manager"] = manager
    api.put_device_fields.side_effect = [None, TimeoutError("ambiguous")]
    with pytest.raises(TimeoutError, match="ambiguous"):
        await entity.async_set_preset_mode("sleep")
    assert api.put_device_fields.await_count == 2
    assert entity.preset_mode == "home"
    manager.request_recovery.assert_called_once_with("unit-1")
    assert (
        hass.data[DOMAIN]["climate-entry"]["sleep_exit_pending"]["unit-1"]
        == "unverified"
    )
    assert hass.data[DOMAIN]["climate-entry"].get("sleep_tracking") is None


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
    # The automatic cleanup now requires session-owned ON/OFF evidence
    # inside the device lock; a bare coordinator flag is intentionally unsafe.
    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import SleepTracking

    hass.data[DOMAIN]["climate-entry"]["sleep_tracking"] = {
        "unit-1": SleepTracking(
            last_scenary="sleep",
            sleep_started_at_utc=dt_util.utcnow(),
            observed_on=True,
            consecutive_off_reports=2,
        )
    }
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
    try:
        assert not manual_sleep.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(cleanup, manual_sleep), timeout=3)

    api.async_set_scenary.assert_awaited_once_with("unit-1", "occupied", retry=False)
    # With cached raw Sleep, a manual rearm still uses occupied → sleep.
    assert [
        call.args[1]["device"]["scenary"]
        for call in api.put_device_fields.await_args_list
    ] == ["occupied", "sleep"]
    assert entity.preset_mode == "sleep"
    # The automatic Home path schedules a one-second coordinator refresh
    # without a live SyncManager. The official HA harness requires this
    # fixture-owned timer to be cancelled at test teardown.
    if cancel := hass.data[DOMAIN]["climate-entry"].get("pending_refresh"):
        cancel()


def _arm_current_sleep_off_evidence(hass) -> None:
    """Make a cleanup eligible so these tests exercise the intent/pending fence."""
    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import SleepTracking

    hass.data[DOMAIN]["climate-entry"]["sleep_tracking"] = {
        "unit-1": SleepTracking(
            last_scenary="sleep",
            sleep_started_at_utc=dt_util.utcnow(),
            observed_on=True,
            consecutive_off_reports=2,
        )
    }


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
    manager.request_recovery.assert_called_once_with("unit-1")
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
    manager.request_recovery.assert_called_once_with("unit-1")

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

    async def paused_send(payload: dict[str, Any]) -> None:
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

    async def paused_preparation(_reason: str) -> None:
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

    async def prepare(_reason: str) -> None:
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

    async def paused_send(payload: dict[str, Any]) -> None:
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



async def test_hvac_off_supersedes_older_turn_on_during_sleep_preparation(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OFF mode request must stop a previously queued wake P1=1."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_scene(_reason: str) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(entity, "_ensure_occupied_before_active_action", wait_for_scene)
    pending_on = asyncio.create_task(entity.async_turn_on())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await entity.async_set_hvac_mode(HVACMode.OFF)
    finally:
        release.set()
    await asyncio.wait_for(pending_on, timeout=3)

    events = [c.args[0]["event"] for c in api.send_event.await_args_list]
    assert [(e["option"], e["value"]) for e in events] == [("P1", 0)]
    assert entity._overlay_value("power", "0") == "0"


async def test_hvac_active_mode_is_fenced_by_newer_turn_off_during_preparation(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed mode selection cannot power on after a newer explicit OFF."""
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power="0")
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_scene(_reason: str) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(entity, "_ensure_occupied_before_active_action", wait_for_scene)
    pending_mode = asyncio.create_task(entity.async_set_hvac_mode(HVACMode.HEAT))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await entity.async_turn_off()
    finally:
        release.set()
    await asyncio.wait_for(pending_mode, timeout=3)

    api.send_event.assert_not_awaited()
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

    async def wait_for_scene(_reason: str) -> None:
        entered.set()
        await release.wait()

    if first == "switch":
        monkeypatch.setattr(switch, "_ensure_occupied_before_active_action", wait_for_scene)
        older = switch._fallback_turn_on
    else:
        monkeypatch.setattr(climate, "_ensure_occupied_before_active_action", wait_for_scene)
        older = climate.async_turn_on

    pending_wake = asyncio.create_task(older())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await climate.async_set_hvac_mode(HVACMode.OFF)
    finally:
        release.set()
    await asyncio.wait_for(pending_wake, timeout=3)

    events = [c.args[0]["event"] for c in api.send_event.await_args_list]
    assert [(e["option"], e["value"]) for e in events] == [("P1", 0)]


@pytest.mark.parametrize("order", ["off_then_active_mode", "active_mode_then_off"])
async def test_hvac_mode_and_power_commands_serialize_opposite_intents(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    """Mode activation cannot lose an ON behind a stale GET or a later OFF."""
    initial = "1" if order == "off_then_active_mode" else "0"
    entity, api = _climate(hass, dummy_coordinator_factory, monkeypatch, power=initial)
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[tuple[str, Any]] = []

    async def paused_p1(payload: dict[str, Any]) -> None:
        event = payload["event"]
        calls.append((event["option"], event["value"]))
        if len(calls) == 1:
            entered.set()
            await release.wait()

    api.send_event.side_effect = paused_p1
    first = (
        entity.async_turn_off
        if order == "off_then_active_mode"
        else lambda: entity.async_set_hvac_mode(HVACMode.HEAT)
    )
    second = (
        (lambda: entity.async_set_hvac_mode(HVACMode.HEAT))
        if order == "off_then_active_mode"
        else entity.async_set_hvac_mode
    )

    first_task = asyncio.create_task(first())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_task = asyncio.create_task(
            second() if order == "off_then_active_mode" else second(HVACMode.OFF)
        )
        await asyncio.sleep(0)
        assert not second_task.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=3)

    if order == "off_then_active_mode":
        assert calls == [("P1", 0), ("P1", 1), ("P2", "2")]
        assert entity._overlay_value("power", initial) == "1"
    else:
        assert calls == [("P1", 1), ("P2", "2"), ("P1", 0)]
        assert entity._overlay_value("power", initial) == "0"


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
        }
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
        manager.request_recovery.assert_called_once_with("unit-1")
        climate_module.schedule_post_write_refresh.assert_not_called()
    else:
        climate_module.schedule_post_write_refresh.assert_called_once_with(
            hass, entity.coordinator, entry_id="climate-entry"
        )


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
        }
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
