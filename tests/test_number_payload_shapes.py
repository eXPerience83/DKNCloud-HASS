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
        self, device_id: str, payload: dict[str, Any], **kwargs
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

    async def reject_write(
        _device_id: str, _payload: dict[str, Any], **_kwargs
    ) -> None:
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

    async def ambiguous_put(
        _device_id: str, payload: dict[str, Any], **_kwargs
    ) -> None:
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

    async def unknown_write(
        _device_id: str, _payload: dict[str, Any], **_kwargs
    ) -> None:
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


@pytest.mark.parametrize("outcome", ["accepted", "ambiguous", "cooldown"])
async def test_sleep_duration_write_invalidates_old_clock(
    hass, dummy_coordinator_factory, outcome
):
    """An old snapshot must not keep the deadline usable after a possible write."""
    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin.airzone_api import CommandNotSentCooldown
    from custom_components.airzoneclouddaikin.sleep import SleepTracking

    entity, api = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data={"sleep_time": 30, "scenary": "sleep", "power": "0"},
    )
    now = dt_util.utcnow()
    tracking = SleepTracking(
        last_scenary="sleep",
        sleep_started_at_utc=now,
        cleanup_started_at_utc=now,
        start_known=True,
        expected_minutes=30,
        expiry_off_reports=2,
        expiry_revisions=(0, 0, 0),
    )
    hass.data[DOMAIN]["entry"]["sleep_tracking"] = {"dev1": tracking}
    error = {"ambiguous": TimeoutError(), "cooldown": CommandNotSentCooldown(0)}.get(
        outcome
    )
    if error is not None:

        async def rejected(*args, **kwargs):
            raise error

        api.put_device_fields = rejected
        with pytest.raises(type(error)):
            await entity.async_set_native_value(40)
    else:
        await entity.async_set_native_value(40)

    assert tracking.duration_invalidated is (outcome != "cooldown")
    assert tracking.expiry_verified is (outcome == "cooldown")


@pytest.mark.parametrize("http_status", [204, 503, 422])
async def test_sleep_duration_loopback_and_old_get(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
    socket_enabled,
    aiohttp_server,
    http_status,
):
    """A traced mutation and stale telemetry must use the lock-owned deadline."""
    from datetime import timedelta
    from types import SimpleNamespace

    from aiohttp import ClientResponseError, web
    from homeassistant.util import dt as dt_util

    from custom_components.airzoneclouddaikin import (
        _derive_state,
        airzone_api,
        async_create_runtime_session,
    )
    from custom_components.airzoneclouddaikin.sleep import (
        SleepTracking,
        sleep_cleanup_due,
    )

    old_device = {"id": "dev1", "sleep_time": 30, "scenary": "sleep", "power": "0"}
    entity, _ = _make_entity(
        hass,
        dummy_coordinator_factory,
        DKNSleepTimeNumber,
        device_data=old_device,
    )
    bucket = hass.data[DOMAIN]["entry"]
    started = dt_util.utcnow() - timedelta(minutes=40)
    tracking = SleepTracking(
        last_scenary="sleep",
        sleep_started_at_utc=started,
        cleanup_started_at_utc=started,
        expected_minutes=30,
        start_known=True,
        expiry_off_reports=2,
        expiry_revisions=(0, 0, 0),
    )
    bucket["sleep_tracking"] = {"dev1": tracking}
    writes = []

    async def respond(request):
        writes.append(await request.json())
        assert bucket["device_locks"]["dev1"].locked()
        return web.Response(status=http_status)

    app = web.Application()
    app.router.add_put("/devices/dev1", respond)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))
    entity._api = airzone_api.AirzoneAPI(
        "synthetic@example.invalid",
        async_create_runtime_session(hass),
        token="synthetic",
    )

    if http_status == 204:
        await entity.async_set_native_value(40)
    else:
        with pytest.raises(ClientResponseError) as error:
            await entity.async_set_native_value(40)
        assert error.value.status == http_status

    data = {"dev1": old_device}
    _derive_state(
        hass,
        SimpleNamespace(entry_id="entry", options={}),
        data,
        fresh_power_ids={"dev1"},
        fresh_scenary_ids={"dev1"},
        power_observations={"dev1": (1, dt_util.utcnow())},
    )
    assert writes == [{"sleep_time": 40}]
    assert tracking.duration_invalidated == (http_status != 422)
    assert (sleep_cleanup_due(tracking) is None) == (http_status != 422)
    assert data["dev1"]["sleep_expiry_verified"] == (http_status == 422)


@pytest.mark.parametrize(
    "outcome",
    [
        "accepted",
        "get_401_before_dispatch",
        "get_401_in_flight",
        "put_401",
        "cancel_before",
        "cancel_after",
        "total_timeout",
    ],
)
async def test_sleep_begins_during_duration_loopback(
    hass,
    dummy_coordinator_factory,
    monkeypatch,
    socket_enabled,
    aiohttp_server,
    outcome,
):
    """Remote Sleep can begin while the traced edit owns the write lock."""
    from types import SimpleNamespace

    from aiohttp import ClientResponseError, web
    from homeassistant.exceptions import ConfigEntryAuthFailed

    from custom_components.airzoneclouddaikin import (
        _derive_state,
        airzone_api,
        async_create_runtime_session,
    )

    dev = {"id": "dev1", "scenary": "occupied", "sleep_time": 30, "power": "0"}
    entity, _ = _make_entity(
        hass, dummy_coordinator_factory, DKNSleepTimeNumber, device_data=dev
    )
    entry = SimpleNamespace(entry_id="entry", options={})
    data = {"dev1": dev}
    _derive_state(hass, entry, data, fresh_scenary_ids={"dev1"})
    arrived, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def put(request):
        requests.append("PUT")
        assert await request.json() == {"sleep_time": 40}
        arrived.set()
        await release.wait()
        return web.Response(status=401 if outcome == "put_401" else 204)

    async def get_unauthorized(_request):
        requests.append("GET")
        return web.Response(status=401)

    app = web.Application()
    app.router.add_put("/devices/dev1", put)
    app.router.add_get("/devices", get_unauthorized)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))
    session = async_create_runtime_session(hass)
    entity._api = api = airzone_api.AirzoneAPI(
        "synthetic@example.invalid",
        session,
        token="synthetic",
    )
    api.on_write_auth_failure = Mock()
    connect = session.connector.connect

    async def hold_put_connection(request, *args, **kwargs):
        if request.method == "PUT":
            arrived.set()
            await release.wait()
        return await connect(request, *args, **kwargs)

    if outcome in {"get_401_before_dispatch", "cancel_before", "total_timeout"}:
        monkeypatch.setattr(session.connector, "connect", hold_put_connection)
    if outcome == "total_timeout":
        monkeypatch.setattr(airzone_api, "REQUEST_TIMEOUT", 0.1)
    task = asyncio.create_task(entity.async_set_native_value(40))
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        dev["scenary"] = "sleep"
        _derive_state(hass, entry, data, fresh_scenary_ids={"dev1"})
        tracking = hass.data[DOMAIN]["entry"]["sleep_tracking"]["dev1"]
        assert not tracking.duration_invalidated
        if outcome == "total_timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, 2)
            assert requests == []
        elif outcome.startswith("cancel"):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert requests == (["PUT"] if outcome == "cancel_after" else [])
        elif outcome.startswith("get_401"):
            with pytest.raises(ClientResponseError) as get_error:
                await api.fetch_devices("fake", retry=False)
            assert get_error.value.status == 401
            with pytest.raises(airzone_api._PutInterruptedByReauth) as error:
                await asyncio.wait_for(task, 2)
            in_flight = outcome == "get_401_in_flight"
            assert airzone_api.device_write_rejected(
                error.value, in_flight=in_flight
            ) == (not in_flight)
            assert requests == (["PUT", "GET"] if in_flight else ["GET"])
            api.on_write_auth_failure.assert_not_called()
        else:
            release.set()
            if outcome == "put_401":
                with pytest.raises(ConfigEntryAuthFailed) as error:
                    await asyncio.wait_for(task, 2)
                assert type(error.value) is ConfigEntryAuthFailed
                assert airzone_api.device_write_rejected(error.value, in_flight=True)
                api.on_write_auth_failure.assert_called_once_with()
            else:
                await asyncio.wait_for(task, 2)
                api.on_write_auth_failure.assert_not_called()
            assert requests == ["PUT"]
        assert tracking.duration_invalidated == (
            outcome in {"accepted", "get_401_in_flight", "cancel_after"}
        )
        _derive_state(
            hass, entry, data, fresh_scenary_ids={"dev1"}, fresh_duration_ids={"dev1"}
        )
        assert tracking.duration_invalidated == (
            outcome in {"accepted", "get_401_in_flight", "cancel_after"}
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
