"""Background live-state refresh scheduling for DKN Cloud devices."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.core import HomeAssistant

from .airzone_api import AirzoneAPI

_LOGGER = logging.getLogger(__name__)

_LIVE_REFRESH_MAX_CONCURRENCY = 2


def queue_live_refresh(
    hass: HomeAssistant,
    bucket: dict[str, Any],
    api: AirzoneAPI,
    device_ids: Iterable[str],
    on_auth_failure: Callable[[], None],
) -> None:
    """Queue best-effort live refreshes without blocking coordinator snapshots."""
    pending: set[str] = bucket.setdefault("live_refresh_pending_ids", set())
    inflight: set[str] = bucket.setdefault("live_refresh_inflight_ids", set())

    pending.update(
        device_id
        for raw_device_id in device_ids
        if (device_id := str(raw_device_id).strip()) and device_id not in inflight
    )
    if not pending:
        return

    task: asyncio.Task[None] | None = bucket.get("live_refresh_task")
    if task is not None and not task.done():
        return

    bucket["live_refresh_task"] = hass.async_create_task(
        _async_live_refresh_worker(bucket, api, on_auth_failure)
    )


async def _async_live_refresh_worker(
    bucket: dict[str, Any],
    api: AirzoneAPI,
    on_auth_failure: Callable[[], None],
) -> None:
    """Drain queued device refreshes with bounded concurrency and coalescing."""
    pending: set[str] = bucket.setdefault("live_refresh_pending_ids", set())
    inflight: set[str] = bucket.setdefault("live_refresh_inflight_ids", set())
    semaphore = asyncio.Semaphore(_LIVE_REFRESH_MAX_CONCURRENCY)
    auth_failed = False

    async def _refresh_one(device_id: str) -> bool:
        nonlocal auth_failed

        async with semaphore:
            if auth_failed:
                return False
            try:
                await api.request_device_info(device_id)
                return True
            except asyncio.CancelledError:
                raise
            except ClientResponseError as err:
                if err.status == 401 and not auth_failed:
                    auth_failed = True
                    on_auth_failure()
                return False
            except Exception:  # noqa: BLE001
                return False

    try:
        while pending and not auth_failed:
            batch = tuple(sorted(pending - inflight))
            if not batch:
                break

            pending.difference_update(batch)
            inflight.update(batch)
            try:
                results = await asyncio.gather(
                    *(_refresh_one(device_id) for device_id in batch)
                )
            finally:
                inflight.difference_update(batch)

            failures = sum(not result for result in results)
            if failures:
                _LOGGER.debug(
                    "Live device information refresh failed for %d device(s).",
                    failures,
                )

        if auth_failed:
            pending.clear()
    finally:
        inflight.clear()
        current = asyncio.current_task()
        if bucket.get("live_refresh_task") is current:
            bucket["live_refresh_task"] = None


def cancel_live_refresh(bucket: dict[str, Any]) -> None:
    """Cancel queued/background live refresh work for an unloading entry."""
    task: asyncio.Task[None] | None = bucket.get("live_refresh_task")
    if task is not None and not task.done():
        task.cancel()
    pending = bucket.get("live_refresh_pending_ids")
    if isinstance(pending, set):
        pending.clear()
    inflight = bucket.get("live_refresh_inflight_ids")
    if isinstance(inflight, set):
        inflight.clear()
