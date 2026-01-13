"""Notification formatting and coordinator listener coverage."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for key in list(sys.modules):
    if key.startswith("custom_components.airzoneclouddaikin"):
        sys.modules.pop(key, None)

UTC = getattr(datetime, "UTC", timezone.utc)  # noqa: UP017

try:
    from aiohttp import (  # type: ignore[import-untyped]
        ClientConnectorError,
        ClientResponseError,
        ClientSession,
        ClientTimeout,
    )
except (ImportError, ModuleNotFoundError):  # pragma: no cover - handled by CI deps
    aiohttp_module = types.ModuleType("aiohttp")
    sys.modules["aiohttp"] = aiohttp_module

    class ClientResponseError(Exception):
        """Minimal aiohttp ClientResponseError placeholder."""

    class ClientConnectorError(Exception):
        """Minimal aiohttp ClientConnectorError placeholder."""

    class ClientSession:  # pragma: no cover - placeholder
        pass

    class ClientTimeout:
        """Minimal aiohttp ClientTimeout placeholder."""

        def __init__(self, *_: Any, **__: Any) -> None:
            return None

    aiohttp_module.ClientResponseError = ClientResponseError
    aiohttp_module.ClientConnectorError = ClientConnectorError
    aiohttp_module.ClientSession = ClientSession
    aiohttp_module.ClientTimeout = ClientTimeout

# ---------------------------------------------------------------------------
# Minimal Home Assistant shims so the package import works without HA
# ---------------------------------------------------------------------------
ha_module = types.ModuleType("homeassistant")
sys.modules["homeassistant"] = ha_module

components_module = types.ModuleType("homeassistant.components")
persistent_notification_module = types.ModuleType(
    "homeassistant.components.persistent_notification"
)
components_module.persistent_notification = persistent_notification_module
sys.modules["homeassistant.components"] = components_module
sys.modules["homeassistant.components.persistent_notification"] = (
    persistent_notification_module
)
ha_module.components = components_module

config_entries_module = types.ModuleType("homeassistant.config_entries")
config_entries_module.SOURCE_REAUTH = "reauth"


class ConfigEntry:  # pragma: no cover - used for import wiring
    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.unique_id: str | None = None
        self.version = 1
        self._unload: list[Any] = []

    def async_on_unload(self, func: Any) -> None:
        self._unload.append(func)

    def add_update_listener(self, listener: Any) -> Any:
        return listener


config_entries_module.ConfigEntry = ConfigEntry
sys.modules["homeassistant.config_entries"] = config_entries_module
ha_module.config_entries = config_entries_module

const_module = types.ModuleType("homeassistant.const")
const_module.CONF_USERNAME = "username"
sys.modules["homeassistant.const"] = const_module
ha_module.const = const_module

core_module = types.ModuleType("homeassistant.core")


class HomeAssistant:  # pragma: no cover - placeholder
    pass


core_module.HomeAssistant = HomeAssistant
sys.modules["homeassistant.core"] = core_module
ha_module.core = core_module

exceptions_module = types.ModuleType("homeassistant.exceptions")


class HomeAssistantError(Exception):
    """Minimal Home Assistant error placeholder."""


class ConfigEntryAuthFailed(HomeAssistantError):
    """Minimal auth failure placeholder."""


exceptions_module.HomeAssistantError = HomeAssistantError
exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed
sys.modules["homeassistant.exceptions"] = exceptions_module
ha_module.exceptions = exceptions_module

helpers_module = types.ModuleType("homeassistant.helpers")
sys.modules["homeassistant.helpers"] = helpers_module

aiohttp_client_module = types.ModuleType("homeassistant.helpers.aiohttp_client")


def async_get_clientsession(*_: Any, **__: Any) -> None:
    return None


aiohttp_client_module.async_get_clientsession = async_get_clientsession
helpers_module.aiohttp_client = aiohttp_client_module
sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client_module

event_module = types.ModuleType("homeassistant.helpers.event")


async def async_call_later(*_: Any, **__: Any) -> None:
    return None


event_module.async_call_later = async_call_later
helpers_module.event = event_module
sys.modules["homeassistant.helpers.event"] = event_module

translation_module = types.ModuleType("homeassistant.helpers.translation")


async def async_get_translations(*_: Any, **__: Any) -> dict[str, str]:
    return {}


translation_module.async_get_translations = async_get_translations
helpers_module.translation = translation_module
sys.modules["homeassistant.helpers.translation"] = translation_module

update_coordinator_module = types.ModuleType("homeassistant.helpers.update_coordinator")


class UpdateFailed(Exception):
    """Placeholder for coordinator update failures."""


class DataUpdateCoordinator:
    """Minimal coordinator stub that registers listeners."""

    def __init__(
        self, hass: Any, *_: Any, update_method: Any = None, **__: Any
    ) -> None:
        self.hass = hass
        self.update_method = update_method
        self.data: dict[str, Any] = {}
        self._listeners: list[Any] = []

    async def async_config_entry_first_refresh(self) -> None:
        if self.update_method is not None:
            self.data = await self.update_method()

    def async_add_listener(self, listener: Any) -> Any:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    def async_request_refresh(self) -> None:
        return None

    def __class_getitem__(cls, item: object) -> type:
        return cls


update_coordinator_module.UpdateFailed = UpdateFailed
update_coordinator_module.DataUpdateCoordinator = DataUpdateCoordinator
helpers_module.update_coordinator = update_coordinator_module
sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator_module

util_module = types.ModuleType("homeassistant.util")
dt_module = types.ModuleType("homeassistant.util.dt")
util_module.dt = dt_module
helpers_module.util = util_module
sys.modules["homeassistant.util"] = util_module
sys.modules["homeassistant.util.dt"] = dt_module


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


dt_module.utcnow = utcnow
dt_module.parse_datetime = parse_datetime
dt_module.as_utc = as_utc
dt_module.as_local = as_local

ha_module.helpers = helpers_module

import custom_components.airzoneclouddaikin as integration  # noqa: E402
from custom_components.airzoneclouddaikin.const import (  # noqa: E402
    DOMAIN,
    OFFLINE_DEBOUNCE_SEC,
    ONLINE_BANNER_TTL_SEC,
    PN_KEY_PREFIX,
)


class DummyConfigEntries:
    async def async_forward_entry_setups(self, *_: Any, **__: Any) -> None:
        return None

    async def async_forward_entry_unload(self, *_: Any, **__: Any) -> bool:
        return True

    async def async_reload(self, *_: Any, **__: Any) -> None:
        return None

    def async_entries(self, *_: Any, **__: Any) -> list[Any]:
        return []


class DummyHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config = types.SimpleNamespace(language="en")
        self.config_entries = DummyConfigEntries()


def _make_entry() -> ConfigEntry:
    entry = ConfigEntry()
    entry.data = {const_module.CONF_USERNAME: "user@example.com"}
    entry.options = {"user_token": "token", "scan_interval": 10}
    return entry


def test_fmt_includes_name_in_message() -> None:
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
    assert message == "Last seen — (— minutes ago)."


def test_fmt_warns_and_falls_back_on_malformed_templates(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
async def test_offline_notification_after_debounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = DummyHass()
    entry = _make_entry()

    monkeypatch.setattr(
        integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
    )

    await integration.async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = coordinator._listeners[-1]

    integration.persistent_notification.async_create = Mock()
    integration.persistent_notification.async_dismiss = Mock()

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = DummyHass()
    entry = _make_entry()

    monkeypatch.setattr(
        integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
    )

    await integration.async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = coordinator._listeners[-1]

    integration.persistent_notification.async_create = Mock()
    integration.persistent_notification.async_dismiss = Mock()

    scheduled: list[tuple[float, Any]] = []

    def fake_call_later(hass_arg: Any, delay: float, action: Any) -> Any:
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = DummyHass()
    entry = _make_entry()

    monkeypatch.setattr(
        integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
    )

    await integration.async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = coordinator._listeners[-1]

    integration.persistent_notification.async_create = Mock()
    integration.persistent_notification.async_dismiss = Mock()

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = DummyHass()
    entry = _make_entry()

    monkeypatch.setattr(
        integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
    )

    await integration.async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = coordinator._listeners[-1]

    integration.persistent_notification.async_create = Mock()
    integration.persistent_notification.async_dismiss = Mock()

    events: list[str] = []
    cancels: list[Mock] = []

    def fake_call_later(hass_arg: Any, delay: float, action: Any) -> Any:
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = DummyHass()
    entry = _make_entry()

    monkeypatch.setattr(
        integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
    )

    await integration.async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = coordinator._listeners[-1]

    integration.persistent_notification.async_create = Mock()
    integration.persistent_notification.async_dismiss = Mock()

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
    coordinator.data = {"dev-6": {"name": "Unit 6", "connection_date": old.isoformat()}}

    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
    listener()

    online_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-6:online"
    integration.persistent_notification.async_dismiss.assert_any_call(hass, online_nid)
    cancel.assert_called_once()


@pytest.mark.asyncio
async def test_offline_notification_includes_datetime_connection_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = DummyHass()
    entry = _make_entry()

    monkeypatch.setattr(
        integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
    )

    await integration.async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    listener = coordinator._listeners[-1]

    integration.persistent_notification.async_create = Mock()
    integration.persistent_notification.async_dismiss = Mock()

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
