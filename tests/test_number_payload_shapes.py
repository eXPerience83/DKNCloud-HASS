"""Tests for number entity payload shapes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

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

    async def put_device_fields(
        self, device_id: str, payload: dict[str, Any], **_kwargs
    ) -> None:
        """Record a device-field write."""
        self.calls.append((device_id, payload))


@pytest.fixture(autouse=True)
def disable_post_write_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid delayed refresh timers in payload-shape unit tests."""

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.number.schedule_post_write_refresh",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
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
    hass.data[DOMAIN][entry_id]["coordinator"] = coordinator

    entity = entity_cls(
        coordinator=coordinator,
        api=api,
        entry_id=entry_id,
        device_id=device_id,
    )
    entity.hass = hass
    entity.async_write_ha_state = lambda: None

    return entity, api


@pytest.mark.parametrize(
    ("entity_cls", "field"),
    [
        (DKNSleepTimeNumber, "sleep_time"),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied"),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied"),
    ],
)
def test_number_entity_unique_ids_remain_unchanged(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
) -> None:
    """Inheriting the shared constructor must preserve registered entity IDs."""
    entity, _api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: 30}
    )

    assert entity.unique_id == f"dev1_{field}"


@pytest.mark.asyncio
async def test_failed_number_write_preserves_backend_state(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """A failed write must propagate without applying an optimistic value."""
    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )

    async def reject_write(_device_id: str, _payload: dict[str, Any], **_kwargs) -> None:
        raise RuntimeError("Backend write failed")

    api.put_device_fields = reject_write

    with pytest.raises(RuntimeError, match="Backend write failed"):
        await entity.async_set_native_value(40)

    assert entity.native_value == 30


@pytest.mark.parametrize(
    ("entity_cls", "field", "initial", "value"),
    [
        (DKNSleepTimeNumber, "sleep_time", 30, 40),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 18),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 26, 28),
    ],
)
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_number_ambiguous_put_requests_readback_without_optimistic_success(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
    initial: int,
    value: int,
    failure: type[BaseException],
) -> None:
    """Unknown PUT acceptance should trigger recovery without another write."""
    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )
    requests: list[dict[str, Any]] = []

    async def ambiguous_put(_device_id: str, payload: dict[str, Any], *, on_in_flight):
        on_in_flight(True)
        requests.append(payload)
        raise failure("unknown PUT result")

    api.put_device_fields = ambiguous_put
    manager = Mock()
    hass.data[DOMAIN]["entry"]["sync_manager"] = manager

    with pytest.raises(failure):
        await entity.async_set_native_value(value)

    assert requests == [{field: value}]
    assert entity.native_value == initial
    manager.request_recovery.assert_called_once_with("dev1", require_fresh=True)


async def test_number_known_unsent_cooldown_does_not_request_readback(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """A local cooldown rejection does not have an ambiguous backend outcome."""
    from custom_components.airzoneclouddaikin.airzone_api import (
        CommandNotSentCooldown,
    )

    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )

    async def unsent(_device_id: str, _payload: dict[str, Any], **_kwargs) -> None:
        raise CommandNotSentCooldown(hass.loop.time() + 60)

    api.put_device_fields = unsent
    manager = Mock()
    hass.data[DOMAIN]["entry"]["sync_manager"] = manager

    with pytest.raises(CommandNotSentCooldown):
        await entity.async_set_native_value(40)

    manager.request_recovery.assert_not_called()
    assert entity.native_value == 30


async def test_ambiguous_number_put_without_sync_manager_schedules_refresh(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy coordinator uses a bounded refresh when SyncManager is absent."""
    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )

    async def unknown_write(_device_id: str, _payload: dict[str, Any], **_kwargs) -> None:
        raise TimeoutError("unknown PUT acceptance")

    api.put_device_fields = unknown_write
    scheduled = Mock()
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.helpers.schedule_post_write_refresh",
        scheduled,
    )

    with pytest.raises(TimeoutError):
        await entity.async_set_native_value(40)

    scheduled.assert_called_once_with(hass, entity.coordinator, entry_id="entry")
    assert entity.native_value == 30


@pytest.mark.parametrize(
    ("entity_cls", "field", "initial", "value"),
    [
        (DKNSleepTimeNumber, "sleep_time", 30, 40),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 17),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 26, 27),
    ],
)
@pytest.mark.parametrize("unload_status", ["removed", "closing", "replaced"])
async def test_late_successful_number_put_cannot_update_unloaded_entry(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
    initial: int,
    value: int,
    unload_status: str,
) -> None:
    """An accepted field PUT cannot recreate the bucket after unload/reload."""
    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )
    bucket = hass.data[DOMAIN]["entry"]
    manager = Mock()
    bucket["sync_manager"] = manager
    writes: list[dict[str, Any]] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_put(_device_id: str, payload: dict[str, Any], **_kwargs) -> None:
        writes.append(payload)
        entered.set()
        await release.wait()

    api.put_device_fields = held_put
    state_updates = Mock()
    entity.async_write_ha_state = state_updates
    task = asyncio.create_task(entity.async_set_native_value(value))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if unload_status == "removed":
            hass.data[DOMAIN].pop("entry")
        elif unload_status == "replaced":
            hass.data[DOMAIN]["entry"] = {"sync_manager": Mock()}
        else:
            bucket["closing"] = True
    finally:
        release.set()

    await asyncio.wait_for(task, timeout=3)
    assert writes == [{field: value}]
    assert field not in bucket.get("optimistic", {}).get("dev1", {})
    state_updates.assert_not_called()
    manager.request_recovery.assert_not_called()
    if unload_status == "removed":
        assert "entry" not in hass.data[DOMAIN]
    elif unload_status == "replaced":
        hass.data[DOMAIN]["entry"]["sync_manager"].request_recovery.assert_not_called()


async def test_replaced_entry_ignores_late_ambiguous_number_put(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """An old PUT error cannot request recovery on the reloaded entry."""
    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_error(_device_id: str, _payload: dict[str, Any], **_kwargs) -> None:
        entered.set()
        await release.wait()
        raise TimeoutError("late response lost")

    api.put_device_fields = held_error
    task = asyncio.create_task(entity.async_set_native_value(40))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        new_manager = Mock()
        hass.data[DOMAIN]["entry"] = {"sync_manager": new_manager}
    finally:
        release.set()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=3)
    new_manager.request_recovery.assert_not_called()
    assert "optimistic" not in hass.data[DOMAIN]["entry"]
    assert "uncertain_fields" not in hass.data[DOMAIN]["entry"]


@pytest.mark.parametrize("unload_status", ["removed", "closing"])
async def test_queued_number_put_does_not_send_after_unload(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    unload_status: str,
) -> None:
    """An old queued field PUT must not send after its entry closes."""
    from custom_components.airzoneclouddaikin.helpers import acquire_device_lock

    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )
    bucket = hass.data[DOMAIN]["entry"]
    lock = acquire_device_lock(hass, "entry", "dev1")
    await lock.acquire()
    try:
        task = asyncio.create_task(entity.async_set_native_value(40))
        await asyncio.sleep(0)
        assert not task.done()
        if unload_status == "removed":
            hass.data[DOMAIN].pop("entry")
        else:
            bucket["closing"] = True
    finally:
        lock.release()

    await asyncio.wait_for(task, timeout=3)
    assert api.calls == []
    if unload_status == "removed":
        assert "entry" not in hass.data[DOMAIN]


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

    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import SleepTracking

    tracking = SleepTracking(
        last_scenary="sleep",
        start_known=True,
        expected_minutes=30,
        sleep_started_at_utc=dt_util.utcnow(),
    )
    hass.data[DOMAIN]["entry"]["sleep_tracking"] = {"dev1": tracking}
    assert tracking.end is not None
    await entity.async_set_native_value(40)

    assert api.calls == [("dev1", {"sleep_time": 40})]
    assert tracking.end is None


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
    ("entity_cls", "field", "initial", "first_value"),
    [
        (DKNSleepTimeNumber, "sleep_time", 30, 40),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 18),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 26, 28),
    ],
)
@pytest.mark.parametrize("same_value", [True, False])
async def test_concurrent_number_writes_recheck_state_under_lock(
    hass: HomeAssistant,
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
    entity_cls: type[Any],
    field: str,
    initial: int,
    first_value: int,
    same_value: bool,
) -> None:
    """A queued same value writes once; an opposite request cannot be lost."""
    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_put(device_id: str, payload: dict[str, Any], **_kwargs) -> None:
        api.calls.append((device_id, payload))
        if len(api.calls) == 1:
            entered.set()
            await release.wait()

    api.put_device_fields = paused_put
    first = asyncio.create_task(entity.async_set_native_value(first_value))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_value = first_value if same_value else initial
        second = asyncio.create_task(entity.async_set_native_value(second_value))
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()

    await asyncio.wait_for(asyncio.gather(first, second), timeout=3)
    expected = [("dev1", {field: first_value})]
    if not same_value:
        expected.append(("dev1", {field: initial}))
    assert api.calls == expected
    assert entity.native_value == second_value


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
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30},
    )

    await entity.async_set_native_value(500)

    assert api.calls == [("dev1", {"sleep_time": 120})]


@pytest.mark.parametrize(
    ("entity_cls", "field", "initial", "changed"),
    [
        (DKNSleepTimeNumber, "sleep_time", 30, 60),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 18),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 26, 28),
    ],
)
@pytest.mark.parametrize("outcome", ["timeout", "cancel", "cooldown"])
async def test_explicit_number_restoration_after_failed_put(
    hass, dummy_coordinator_factory, entity_cls, field, initial, changed, outcome
):
    """A cached Number value must not discard a new explicit restoration."""
    from custom_components.airzoneclouddaikin.airzone_api import CommandNotSentCooldown

    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )
    bucket = hass.data[DOMAIN]["entry"]
    manager = bucket["sync_manager"] = Mock()
    await entity.async_set_native_value(initial)
    assert not api.calls

    error = {
        "timeout": TimeoutError("unknown PUT acceptance"),
        "cancel": asyncio.CancelledError("unknown PUT acceptance"),
        "cooldown": CommandNotSentCooldown(hass.loop.time() + 60),
    }[outcome]

    async def fail_once(device_id, payload, *, on_in_flight):
        if outcome != "cooldown":
            on_in_flight(True)
        api.calls.append((device_id, payload))
        raise error

    original_put = api.put_device_fields
    api.put_device_fields = fail_once
    with pytest.raises(type(error)):
        await entity.async_set_native_value(changed)

    assert api.calls == [("dev1", {field: changed})]
    assert entity.native_value == initial
    if outcome == "cooldown":
        manager.request_recovery.assert_not_called()
    else:
        manager.request_recovery.assert_called_once_with("dev1", require_fresh=True)

    api.put_device_fields = original_put
    await entity.async_set_native_value(initial)
    await entity.async_set_native_value(initial)
    expected = [("dev1", {field: changed})]
    if outcome != "cooldown":
        expected.append(("dev1", {field: initial}))
    assert api.calls == expected
    assert entity.native_value == initial


@pytest.mark.parametrize(
    ("entity_cls", "field", "initial", "changed"),
    [
        (DKNSleepTimeNumber, "sleep_time", 30, 60),
        (DKNUnoccupiedHeatMinNumber, "min_temp_unoccupied", 16, 18),
        (DKNUnoccupiedCoolMaxNumber, "max_temp_unoccupied", 26, 28),
    ],
)
@pytest.mark.parametrize("evidence", ["old-get", "push", "partial-get", "fresh-get"])
async def test_number_uncertainty_requires_fresh_applied_field(
    hass,
    dummy_coordinator_factory,
    entity_cls,
    field,
    initial,
    changed,
    evidence,
):
    """An unrelated or older observation cannot resolve this field's uncertainty."""
    from types import SimpleNamespace

    from custom_components.airzoneclouddaikin import _derive_state, _sleep_get_context

    entity, api = _make_entity(
        hass, dummy_coordinator_factory, entity_cls, device_data={field: initial}
    )
    bucket = hass.data[DOMAIN]["entry"]
    bucket["sync_manager"] = Mock()
    entry = SimpleNamespace(entry_id="entry", options={})
    old = _sleep_get_context(bucket)
    original_put = api.put_device_fields

    async def ambiguous(device_id, payload, **_kwargs):
        api.calls.append((device_id, payload))
        raise TimeoutError("PUT acceptance unknown")

    api.put_device_fields = ambiguous
    with pytest.raises(TimeoutError):
        await entity.async_set_native_value(changed)
    current = _sleep_get_context(bucket)
    _derive_state(
        hass,
        entry,
        entity.coordinator.data,
        sleep_gets=(
            None
            if evidence == "push"
            else {"dev1": old if evidence == "old-get" else current}
        ),
        fresh_fields={"dev1": {"power" if evidence == "partial-get" else field}},
        from_push=evidence == "push",
    )
    api.put_device_fields = original_put
    await entity.async_set_native_value(initial)
    expected = 1 if evidence == "fresh-get" else 2
    assert len(api.calls) == expected
    await entity.async_set_native_value(initial)
    assert len(api.calls) == expected
