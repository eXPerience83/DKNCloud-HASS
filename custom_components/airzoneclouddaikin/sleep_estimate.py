"""Persist local Sleep estimates without restoring control evidence."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _key(device_id: str) -> str:
    return sha256(device_id.encode()).hexdigest()


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(
        hass, 1, f"{DOMAIN}.{entry_id}.sleep_estimates", private=True, atomic_writes=True
    )


def _end(record: Any, now: datetime) -> datetime | None:
    if not isinstance(record, dict):
        return None
    minutes = record.get("minutes")
    start = record.get("start")
    if (
        not isinstance(minutes, int)
        or isinstance(minutes, bool)
        or minutes <= 0
        or not isinstance(start, str)
    ):
        return None
    try:
        started = datetime.fromisoformat(start)
        if started.tzinfo is None or started > now:
            return None
        end = started + timedelta(minutes=minutes)
    except ValueError, OverflowError:
        return None
    return end if end > now else None


class SleepEstimateStore:
    """Entry-local display estimates; never supply runtime session evidence."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _store(hass, entry_id)
        self._records: dict[str, Any] = {}
        self._restored: set[str] = set()
        self._dirty = False

    async def async_load(self, now: datetime) -> None:
        try:
            data = await self._store.async_load()
        except HomeAssistantError, OSError, ValueError, NotImplementedError:
            _LOGGER.warning("Unable to load local Sleep estimates")
            return
        if isinstance(data, dict):
            self._records = {
                key: {"start": record["start"], "minutes": record["minutes"]}
                for key, record in data.items()
                if isinstance(key, str) and _end(record, now) is not None
            }
            self._restored = set(self._records)
            if self._records != data:
                self._changed()

    def _changed(self) -> None:
        self._dirty = True
        self._store.async_delay_save(lambda: dict(self._records), 1)

    def remember(self, device_id: str, start: datetime, minutes: int) -> None:
        """Record only an acknowledged local selection."""
        key = _key(device_id)
        record = {"start": start.isoformat(), "minutes": minutes}
        self._restored.discard(key)
        if self._records.get(key) != record:
            self._records[key] = record
            self._changed()

    def discard(self, device_id: str) -> None:
        key = _key(device_id)
        self._restored.discard(key)
        if self._records.pop(key, None) is not None:
            self._changed()

    def clear(self) -> None:
        if self._records:
            self._records.clear()
            self._restored.clear()
            self._changed()

    def retain_devices(self, device_ids: set[str]) -> None:
        stale = self._records.keys() - {_key(device_id) for device_id in device_ids}
        if stale:
            for key in stale:
                self._records.pop(key)
            self._restored.difference_update(stale)
            self._changed()

    def estimate(
        self,
        device_id: str,
        now: datetime,
        minutes: int | None,
        *,
        current_session_known: bool,
        valid_scene: bool,
    ) -> datetime | None:
        """Return a restored display-only end, or discard unusable records."""
        key = _key(device_id)
        record = self._records.get(key)
        if record is None:
            return None
        end = _end(record, now)
        if not valid_scene or end is None:
            self.discard(device_id)
            return None
        if key not in self._restored:
            if not current_session_known:
                self.discard(device_id)
            return None
        if minutes != record["minutes"]:
            self.discard(device_id)
            return None
        return end

    async def async_flush(self) -> None:
        """Drain the pending Store write before a normal entry reload."""
        if self._dirty:
            try:
                await self._store.async_save(dict(self._records))
            except HomeAssistantError, OSError:
                _LOGGER.warning("Unable to save local Sleep estimates")


async def async_remove_estimates(hass: HomeAssistant, entry_id: str) -> None:
    """Remove only this entry's optional display history."""
    try:
        await _store(hass, entry_id).async_remove()
    except HomeAssistantError, OSError:
        _LOGGER.warning("Unable to remove local Sleep estimates")
