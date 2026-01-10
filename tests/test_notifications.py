"""Notification formatting and coordinator listener coverage."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UTC = getattr(datetime, "UTC", timezone.utc)  # noqa: UP017

aiohttp_module = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))


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
ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

components_module = sys.modules.setdefault(
    "homeassistant.components", types.ModuleType("homeassistant.components")
)
persistent_notification_module = sys.modules.setdefault(
    "homeassistant.components.persistent_notification",
    types.ModuleType("homeassistant.components.persistent_notification"),
)
components_module.persistent_notification = persistent_notification_module

config_entries_module = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
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
ha_module.config_entries = config_entries_module

const_module = sys.modules.setdefault(
    "homeassistant.const", types.ModuleType("homeassistant.const")
)
const_module.CONF_USERNAME = "username"
ha_module.const = const_module

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)


class HomeAssistant:  # pragma: no cover - placeholder
    pass


core_module.HomeAssistant = HomeAssistant
ha_module.core = core_module

exceptions_module = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)


class HomeAssistantError(Exception):
    """Minimal Home Assistant error placeholder."""


class ConfigEntryAuthFailed(HomeAssistantError):
    """Minimal auth failure placeholder."""


exceptions_module.HomeAssistantError = HomeAssistantError
exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed
ha_module.exceptions = exceptions_module

helpers_module = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)

aiohttp_client_module = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)


def async_get_clientsession(*_: Any, **__: Any) -> None:
    return None


aiohttp_client_module.async_get_clientsession = async_get_clientsession
helpers_module.aiohttp_client = aiohttp_client_module

event_module = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)


async def async_call_later(*_: Any, **__: Any) -> None:
    return None


event_module.async_call_later = async_call_later
helpers_module.event = event_module

translation_module = sys.modules.setdefault(
    "homeassistant.helpers.translation",
    types.ModuleType("homeassistant.helpers.translation"),
)


async def async_get_translations(*_: Any, **__: Any) -> dict[str, str]:
    return {}


translation_module.async_get_translations = async_get_translations
helpers_module.translation = translation_module

update_coordinator_module = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


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

util_module = sys.modules.setdefault(
    "homeassistant.util", types.ModuleType("homeassistant.util")
)
dt_module = sys.modules.setdefault(
    "homeassistant.util.dt", types.ModuleType("homeassistant.util.dt")
)
util_module.dt = dt_module
helpers_module.util = util_module
sys.modules.setdefault("homeassistant.util.dt", dt_module)


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
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task


async def _drain_tasks(hass: DummyHass) -> None:
    if hass._tasks:
        await asyncio.gather(*hass._tasks)
        hass._tasks.clear()


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


def test_offline_notification_after_debounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        hass = DummyHass()
        entry = _make_entry()

        monkeypatch.setattr(
            integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
        )

        await integration.async_setup_entry(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        listener = coordinator._listeners[-1]

        persistent_notification_module.async_create = AsyncMock()
        persistent_notification_module.async_dismiss = AsyncMock()

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
        coordinator.data = {
            "dev-1": {"name": "Unit 1", "connection_date": old.isoformat()}
        }

        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
        listener()
        await _drain_tasks(hass)
        persistent_notification_module.async_create.assert_not_called()

        later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
        listener()
        await _drain_tasks(hass)

        expected_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-1"
        persistent_notification_module.async_create.assert_called_once()
        assert (
            persistent_notification_module.async_create.call_args.kwargs[
                "notification_id"
            ]
            == expected_nid
        )

    asyncio.run(_run())


def test_online_notification_dismisses_offline_and_schedules_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        hass = DummyHass()
        entry = _make_entry()

        monkeypatch.setattr(
            integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
        )

        await integration.async_setup_entry(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        listener = coordinator._listeners[-1]

        persistent_notification_module.async_create = AsyncMock()
        persistent_notification_module.async_dismiss = AsyncMock()

        scheduled: list[tuple[float, Any]] = []

        def fake_call_later(hass_arg: Any, delay: float, action: Any) -> Any:
            scheduled.append((delay, action))

            def cancel() -> None:
                return None

            return cancel

        monkeypatch.setattr(integration, "async_call_later", fake_call_later)

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        old = base - timedelta(seconds=integration._OFFLINE_STALE_SECONDS + 10)
        coordinator.data = {
            "dev-2": {"name": "Unit 2", "connection_date": old.isoformat()}
        }

        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
        listener()
        later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
        listener()
        await _drain_tasks(hass)

        online_time = later + timedelta(seconds=10)
        coordinator.data = {
            "dev-2": {"name": "Unit 2", "connection_date": online_time.isoformat()}
        }
        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: online_time)
        listener()
        await _drain_tasks(hass)

        offline_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:dev-2"
        online_nid = f"{offline_nid}:online"

        persistent_notification_module.async_dismiss.assert_any_call(hass, offline_nid)
        assert persistent_notification_module.async_create.call_count >= 2
        assert scheduled
        assert scheduled[0][0] == ONLINE_BANNER_TTL_SEC

        scheduled[0][1](None)
        await _drain_tasks(hass)
        persistent_notification_module.async_dismiss.assert_any_call(hass, online_nid)

    asyncio.run(_run())


def test_listener_never_raises_on_unknown_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        hass = DummyHass()
        entry = _make_entry()

        monkeypatch.setattr(
            integration.AirzoneAPI, "fetch_installations", AsyncMock(return_value=[])
        )

        await integration.async_setup_entry(hass, entry)
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        listener = coordinator._listeners[-1]

        persistent_notification_module.async_create = AsyncMock()
        persistent_notification_module.async_dismiss = AsyncMock()

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
        coordinator.data = {
            "dev-3": {"name": "Unit 3", "connection_date": old.isoformat()}
        }

        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: base)
        listener()
        later = base + timedelta(seconds=OFFLINE_DEBOUNCE_SEC + 1)
        monkeypatch.setattr(integration.dt_util, "utcnow", lambda: later)
        listener()
        await _drain_tasks(hass)

    asyncio.run(_run())
