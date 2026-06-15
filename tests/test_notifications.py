"""Notification formatting, coordinator update, setup, and unload coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from aiohttp import ClientResponseError
from aiohttp.client_reqrep import RequestInfo
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin.config_flow import CONF_SCAN_INTERVAL
from custom_components.airzoneclouddaikin.const import (
    DOMAIN,
    OFFLINE_DEBOUNCE_SEC,
    ONLINE_BANNER_TTL_SEC,
    PN_KEY_PREFIX,
)


class FakeSetupAPI:
    """Fake API used by setup and notification tests."""

    installations: list[dict[str, Any]] = []
    devices_by_installation: dict[str, list[dict[str, Any]]] = {}
    instances: list[FakeSetupAPI] = []

    def __init__(
        self,
        username: str | None,
        session: Any,
        *,
        password: str | None = None,
        token: str | None = None,
    ) -> None:
        self.username = username
        self.session = session
        self.password = password
        self.token = token
        self.fetch_installations = AsyncMock(return_value=list(self.installations))

        async def _fetch_devices(installation_id: str) -> list[dict[str, Any]]:
            return list(self.devices_by_installation.get(str(installation_id), []))

        self.fetch_devices = AsyncMock(side_effect=_fetch_devices)
        self.async_set_scenary = AsyncMock()
        self.send_event = AsyncMock()
        self.put_device_fields = AsyncMock()
        self.instances.append(self)


@pytest.fixture
def setup_api_class(monkeypatch: pytest.MonkeyPatch) -> type[FakeSetupAPI]:
    """Patch integration setup to use a fake API class."""
    FakeSetupAPI.installations = []
    FakeSetupAPI.devices_by_installation = {}
    FakeSetupAPI.instances = []
    monkeypatch.setattr(integration, "AirzoneAPI", FakeSetupAPI)
    return FakeSetupAPI


def _client_response_error(status: int) -> ClientResponseError:
    """Create a ClientResponseError with minimal request info."""
    request_info = RequestInfo(
        URL("https://example.com"),
        "GET",
        CIMultiDict(),
        URL("https://example.com"),
    )
    return ClientResponseError(request_info, history=(), status=status)


def _latest_coordinator_listener(coordinator: Any) -> Callable[[], None]:
    """Return the latest coordinator listener callback across HA internals."""
    listeners = coordinator._listeners
    if isinstance(listeners, dict):
        listener = list(listeners.values())[-1]
        if isinstance(listener, tuple):
            return listener[0]
        return listener
    return listeners[-1]


async def _setup_entry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    setup_api_class: type[FakeSetupAPI],
    *,
    installations: list[dict[str, Any]] | None = None,
    devices_by_installation: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Set up the integration entry with a fake API snapshot."""
    setup_api_class.installations = installations or []
    setup_api_class.devices_by_installation = devices_by_installation or {}
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def test_fmt_includes_name_in_message() -> None:
    """Notification templates should interpolate known placeholders."""
    strings = {
        "offline": {
            "title": "{name} offline",
            "message": "{name} lost connection at {ts_local}.",
        }
    }

    title, message = integration._fmt(
        strings, "offline", "Living Room", "10:01", None, None
    )

    assert title == "Living Room offline"
    assert message == "Living Room lost connection at 10:01."


def test_fmt_missing_values_with_format_specifier() -> None:
    """Missing placeholder values should format to a neutral dash."""
    strings = {
        "offline": {
            "title": "{name} offline",
            "message": "Last seen {last_iso} ({mins:d} minutes ago).",
        }
    }

    title, message = integration._fmt(
        strings, "offline", "Living Room", "10:01", None, None
    )

    assert title == "Living Room offline"
    assert message == "Last seen \u2014 (\u2014 minutes ago)."


def test_fmt_warns_and_falls_back_on_malformed_templates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed templates should fall back once per kind."""
    integration._NOTIFY_FMT_FALLBACK_LOGGED.clear()
    strings = {
        "offline": {
            "title": "Device {name",
            "message": "Lost at {ts_local",
        }
    }

    with caplog.at_level("WARNING"):
        title, message = integration._fmt(
            strings, "offline", "Living Room", "10:01", None, None
        )

    assert title == "DKN Cloud offline notification"
    assert message == "Living Room lost the connection at 10:01."
    assert "Notification templates fell back to defaults for offline" in caplog.text


@pytest.mark.asyncio
async def test_setup_unload_smoke_with_fake_snapshot(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    sample_device: dict[str, Any],
) -> None:
    """Config entry setup should load, store coordinator data, and unload."""
    entry = dkn_config_entry_factory()

    await _setup_entry(
        hass,
        entry,
        setup_api_class,
        installations=[{"installation_id": "install-123"}],
        devices_by_installation={"install-123": [sample_device]},
    )

    bucket = hass.data[DOMAIN][entry.entry_id]
    assert bucket["coordinator"].data == {"dev1": sample_device}
    assert bucket["api"] is setup_api_class.instances[-1]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


@pytest.mark.asyncio
async def test_offline_notification_after_debounce(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline notification should appear only after debounce."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
    coordinator.data = {"dev-1": {"name": "Unit 1", "connection_date": old.isoformat()}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()
    integration.persistent_notification.async_create.assert_not_called()

    later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
    listener()

    expected_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-1"
    integration.persistent_notification.async_create.assert_called_once()
    assert (
        integration.persistent_notification.async_create.call_args.kwargs[
            "notification_id"
        ]
        == expected_nid
    )


@pytest.mark.asyncio
async def test_online_notification_dismisses_offline_and_schedules_banner(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Online transition should dismiss offline and schedule banner cleanup."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    scheduled: list[tuple[float, Any]] = []

    def fake_call_later(hass_arg: Any, delay: float, action: Any) -> Callable[[], None]:
        scheduled.append((delay, action))

        def cancel() -> None:
            return None

        return cancel

    monkeypatch.setattr(integration, "async_call_later", fake_call_later)

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
    coordinator.data = {"dev-2": {"name": "Unit 2", "connection_date": old.isoformat()}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()
    later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
    listener()

    online_time = later + timedelta(seconds=10)
    coordinator.data = {
        "dev-2": {"name": "Unit 2", "connection_date": online_time.isoformat()}
    }
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: online_time)
    listener()

    offline_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-2"
    online_nid = f"{offline_nid}:online"

    integration.persistent_notification.async_dismiss.assert_any_call(hass, offline_nid)
    assert integration.persistent_notification.async_create.call_count == 2
    integration.persistent_notification.async_create.assert_any_call(
        hass,
        message=ANY,
        title=ANY,
        notification_id=offline_nid,
    )
    integration.persistent_notification.async_create.assert_any_call(
        hass,
        message=ANY,
        title=ANY,
        notification_id=online_nid,
    )
    assert scheduled
    assert scheduled[0][0] == ONLINE_BANNER_TTL_SEC

    scheduled[0][1](None)
    integration.persistent_notification.async_dismiss.assert_any_call(hass, online_nid)


@pytest.mark.asyncio
async def test_listener_never_raises_on_unknown_placeholders(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown placeholders in translated templates should not crash listener."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    hass.data[DOMAIN][entry.entry_id]["notify_strings"] = {
        "offline": {
            "title": "Device {unknown}",
            "message": "Went down at {ts_local} ({unknown}).",
        },
        "online": {
            "title": "Back {unknown}",
            "message": "Up at {ts_local} ({unknown}).",
        },
    }

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
    coordinator.data = {"dev-3": {"name": "Unit 3", "connection_date": old.isoformat()}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()
    later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
    listener()


@pytest.mark.asyncio
async def test_online_banner_second_transition_cancels_previous(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second online transition should cancel the previous online banner."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    events: list[str] = []
    cancels: list[Mock] = []

    def fake_call_later(hass_arg: Any, delay: float, action: Any) -> Mock:
        label = f"{len(cancels) + 1}"
        events.append(f"schedule-{label}")

        def _cancel() -> None:
            events.append(f"cancel-{label}")

        cancel = Mock(side_effect=_cancel)
        cancels.append(cancel)
        return cancel

    monkeypatch.setattr(integration, "async_call_later", fake_call_later)

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
    coordinator.data = {"dev-5": {"name": "Unit 5", "connection_date": old.isoformat()}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()
    later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
    listener()

    online_time = later + timedelta(seconds=10)
    coordinator.data = {
        "dev-5": {"name": "Unit 5", "connection_date": online_time.isoformat()}
    }
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: online_time)
    listener()

    notify_state = hass.data[DOMAIN][entry.entry_id]["notify_state"]["dev-5"]
    notify_state["last"] = False

    later_online = online_time + timedelta(seconds=5)
    coordinator.data = {
        "dev-5": {"name": "Unit 5", "connection_date": later_online.isoformat()}
    }
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later_online)
    listener()

    assert events == ["schedule-1", "cancel-1", "schedule-2"]
    assert cancels[0].called
    assert cancels[1].called is False


@pytest.mark.asyncio
async def test_online_to_offline_cancels_online_banner(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Going offline should cancel any pending online banner."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    listener = _latest_coordinator_listener(
        hass.data[DOMAIN][entry.entry_id]["coordinator"]
    )

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    cancel = Mock()
    notify_state = hass.data[DOMAIN][entry.entry_id]["notify_state"]
    notify_state["dev-6"] = {
        "last": True,
        "since_offline": None,
        "notified": False,
        "online_cancel": cancel,
    }

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.data = {"dev-6": {"name": "Unit 6", "connection_date": old.isoformat()}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()

    online_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-6:online"
    integration.persistent_notification.async_dismiss.assert_any_call(hass, online_nid)
    cancel.assert_called_once()


@pytest.mark.asyncio
async def test_removed_device_cleans_notification_state(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed devices should clear notification state and dismiss banners."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    cancel = Mock()
    notify_state = hass.data[DOMAIN][entry.entry_id]["notify_state"]
    notify_state["dev-removed"] = {
        "last": True,
        "since_offline": None,
        "notified": False,
        "online_cancel": cancel,
    }

    coordinator.data = {"dev-7": {"name": "Unit 7", "connection_date": None}}
    listener()

    assert "dev-removed" not in notify_state
    cancel.assert_called_once()
    removed_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-removed"
    integration.persistent_notification.async_dismiss.assert_any_call(hass, removed_nid)
    integration.persistent_notification.async_dismiss.assert_any_call(
        hass, f"{removed_nid}:online"
    )


@pytest.mark.asyncio
async def test_removed_device_cleanup_runs_on_empty_data(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty data should remove stale notify state, but None data should not."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    cancel_empty = Mock()
    notify_state = hass.data[DOMAIN][entry.entry_id]["notify_state"]
    notify_state["dev-removed"] = {
        "last": True,
        "since_offline": None,
        "notified": False,
        "online_cancel": cancel_empty,
    }

    coordinator.data = {}
    listener()
    assert "dev-removed" not in notify_state
    cancel_empty.assert_called_once()

    cancel_none = Mock()
    notify_state["dev-keep"] = {
        "last": True,
        "since_offline": None,
        "notified": False,
        "online_cancel": cancel_none,
    }

    coordinator.data = None
    listener()
    assert "dev-keep" in notify_state
    cancel_none.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_cache_clears_on_last_unload(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
) -> None:
    """Malformed-template warning cache should clear after the last unload."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)

    integration._NOTIFY_FMT_FALLBACK_LOGGED.clear()
    strings = {
        "offline": {
            "title": "Device {name",
            "message": "Lost at {ts_local",
        }
    }
    integration._fmt(strings, "offline", "Living Room", "10:01", None, None)
    assert integration._NOTIFY_FMT_FALLBACK_LOGGED

    unload_ok = await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert unload_ok
    assert not integration._NOTIFY_FMT_FALLBACK_LOGGED


@pytest.mark.asyncio
async def test_offline_notification_includes_datetime_connection_date(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Datetime connection_date values should be included in notification text."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    hass.data[DOMAIN][entry.entry_id]["notify_strings"] = {
        "offline": {
            "title": "{name} offline",
            "message": "Last {last_iso} ({mins} minutes ago).",
        }
    }

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 300)
    coordinator.data = {"dev-4": {"name": "Unit 4", "connection_date": old}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()

    later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
    listener()

    assert integration.persistent_notification.async_create.called
    message = integration.persistent_notification.async_create.call_args.kwargs[
        "message"
    ]
    assert old.isoformat() in message
    assert "minutes ago" in message


@pytest.mark.asyncio
async def test_async_update_data_uses_installation_id_over_relation_id(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Coordinator refresh should use installation_id, not relation id."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    fetched: list[str] = []

    class DummyAPI:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [{"id": "relation-1", "installation_id": "install-1"}]

        async def fetch_devices(self, inst_id: str) -> list[dict[str, Any]]:
            fetched.append(inst_id)
            return [{"id": "dev-a", "name": "Unit A", "scenary": "home"}]

    data = await integration._async_update_data(hass, entry, DummyAPI())

    assert fetched == ["install-1"]
    assert set(data) == {"dev-a"}


@pytest.mark.asyncio
async def test_async_update_data_raises_on_initial_partial_installation_error(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Initial partial installation errors should fail the coordinator refresh."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class DummyAPI:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [
                {"installation_id": "inst-a"},
                {"installation_id": "inst-b"},
            ]

        async def fetch_devices(self, inst_id: str) -> list[dict[str, Any]]:
            if inst_id == "inst-a":
                raise RuntimeError("temporary")
            return [{"id": "dev-b", "name": "Unit B", "scenary": "home"}]

    with pytest.raises(UpdateFailed, match="partial installation refresh"):
        await integration._async_update_data(hass, entry, DummyAPI())


@pytest.mark.asyncio
async def test_async_update_data_preserves_failed_installation_from_previous_snapshot(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Later partial errors should keep the last snapshot for failed installs."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class DummyAPIOk:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [{"installation_id": "inst-a"}, {"installation_id": "inst-b"}]

        async def fetch_devices(self, inst_id: str) -> list[dict[str, Any]]:
            if inst_id == "inst-a":
                return [{"id": "dev-a", "name": "Unit A", "scenary": "home"}]
            return [{"id": "dev-b", "name": "Unit B", "scenary": "home"}]

    first = await integration._async_update_data(hass, entry, DummyAPIOk())
    assert set(first) == {"dev-a", "dev-b"}

    class DummyAPIPartial:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [{"installation_id": "inst-a"}, {"installation_id": "inst-b"}]

        async def fetch_devices(self, inst_id: str) -> list[dict[str, Any]]:
            if inst_id == "inst-a":
                raise RuntimeError("temporary")
            return [{"id": "dev-b", "name": "Unit B2", "scenary": "home"}]

    second = await integration._async_update_data(hass, entry, DummyAPIPartial())

    assert set(second) == {"dev-a", "dev-b"}
    assert second["dev-a"]["name"] == "Unit A"
    assert second["dev-b"]["name"] == "Unit B2"


@pytest.mark.asyncio
async def test_async_update_data_propagates_cancelled_fetch(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Cancelled per-installation fetches should propagate cancellation."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class DummyAPI:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [{"installation_id": "inst-cancel"}]

        async def fetch_devices(self, _inst_id: str) -> list[dict[str, Any]]:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await integration._async_update_data(hass, entry, DummyAPI())


@pytest.mark.asyncio
async def test_async_update_data_401_from_one_installation_triggers_reauth(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """A 401 from one installation should request reauth once."""
    entry = dkn_config_entry_factory(data={CONF_USERNAME: "user@example.com"})
    entry.add_to_hass(hass)

    class DummyAPI:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [
                {"installation_id": "inst-401"},
                {"installation_id": "inst-ok"},
            ]

        async def fetch_devices(self, inst_id: str) -> list[dict[str, Any]]:
            if inst_id == "inst-401":
                raise _client_response_error(401)
            return [{"id": "dev-ok", "name": "Unit OK", "scenary": "home"}]

    with pytest.raises(UpdateFailed, match=r"Authentication required \(401\)"):
        await integration._async_update_data(hass, entry, DummyAPI())

    await hass.async_block_till_done()

    bucket = hass.data[DOMAIN][entry.entry_id]
    assert bucket["reauth_requested"] is True


@pytest.mark.asyncio
async def test_removed_cleanup_keeps_failed_installation_devices_only(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    setup_api_class: type[FakeSetupAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed-device cleanup should spare devices from failed installations."""
    entry = dkn_config_entry_factory()
    await _setup_entry(hass, entry, setup_api_class)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = _latest_coordinator_listener(coordinator)

    monkeypatch.setattr(integration.persistent_notification, "async_create", Mock())
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", Mock())

    bucket = hass.data[DOMAIN][entry.entry_id]
    bucket["last_update_had_install_errors"] = True
    bucket["failed_installations_last_update"] = {"inst-b"}
    bucket["device_installation_map"] = {
        "dev-a": "inst-a",
        "dev-b": "inst-b",
    }

    cancel_a = Mock()
    cancel_b = Mock()
    notify_state = bucket["notify_state"]
    notify_state["dev-a"] = {
        "last": True,
        "since_offline": None,
        "notified": False,
        "online_cancel": cancel_a,
    }
    notify_state["dev-b"] = {
        "last": True,
        "since_offline": None,
        "notified": False,
        "online_cancel": cancel_b,
    }

    coordinator.data = {}
    listener()

    assert "dev-a" not in notify_state
    assert "dev-b" in notify_state
    cancel_a.assert_called_once()
    cancel_b.assert_not_called()


@pytest.mark.asyncio
async def test_async_update_data_prunes_removed_installation_cache_state(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Removed installations should prune cached device/install mappings."""
    entry = dkn_config_entry_factory(options={CONF_SCAN_INTERVAL: 10})
    entry.add_to_hass(hass)

    class DummyAPIInitial:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [{"installation_id": "inst-a"}, {"installation_id": "inst-b"}]

        async def fetch_devices(self, inst_id: str) -> list[dict[str, Any]]:
            if inst_id == "inst-a":
                return [{"id": "dev-a", "name": "Unit A", "scenary": "home"}]
            return [{"id": "dev-b", "name": "Unit B", "scenary": "home"}]

    await integration._async_update_data(hass, entry, DummyAPIInitial())

    class DummyAPIRemovedA:
        async def fetch_installations(self) -> list[dict[str, Any]]:
            return [{"installation_id": "inst-b"}]

        async def fetch_devices(self, _inst_id: str) -> list[dict[str, Any]]:
            return [{"id": "dev-b", "name": "Unit B2", "scenary": "home"}]

    data = await integration._async_update_data(hass, entry, DummyAPIRemovedA())
    bucket = hass.data[DOMAIN][entry.entry_id]

    assert set(data) == {"dev-b"}
    assert "inst-a" not in bucket["last_devices_by_inst"]
    assert "dev-a" not in bucket["device_installation_map"]
