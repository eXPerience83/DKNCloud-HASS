"""Entry-scoped synchronization ownership and anonymized health reporting."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.util import dt as dt_util

from .pusher_client import PusherProtocolError
from .recovery_pool import RecoveryPool
from .request_budget import RequestBudget
from .sync_scheduler import FALLBACK_READ_DELAY, InformationScheduler
from .sync_state import SyncState

_LOGGER = logging.getLogger(__name__)


class ObsoleteSnapshot(Exception):
    """A connection-generation change requires another snapshot, not backoff."""


class SyncManager:
    """Connect information cycles, recovery reads and a single push supervisor.

    Connection creation and vendor event decoding are supplied by the adapter.
    No payload, channel name or account identity is included in diagnostics.
    """

    def __init__(
        self,
        initial: dict[str, dict[str, Any]],
        installations: dict[str, str],
        request: Callable[[str], Awaitable[Any]],
        fetch: Callable[[str], Awaitable[list[dict[str, Any]]]],
        publish: Callable[[dict[str, dict[str, Any]], bool], None],
        reauth: Callable[[], None],
        locks: Callable[[str], asyncio.Lock],
        *,
        connect: Callable[[SyncManager], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
        sleep=asyncio.sleep,
        discover=None,
        on_topology_change=None,
    ) -> None:
        self.state = SyncState()
        self.state.snapshot(list(initial.values()), self.state.checkpoint())
        self._installations = dict(installations)
        self._request, self._fetch = request, fetch
        self._publish, self._reauth, self._locks = publish, reauth, locks
        self._connect, self._clock = connect, clock
        self._sleep = sleep
        self._information_ready: dict[str, float] = {}
        self._discover = discover
        self._on_topology_change = on_topology_change
        self._discovery_lock = asyncio.Lock()
        self._last_discovery = None
        self._topology_task = None
        self._limit = asyncio.Semaphore(2)
        self._budget = RequestBudget(clock, sleep)
        self._pool = RecoveryPool(self._read, clock, timed_fetch=self._read)
        self._tasks: list[asyncio.Task] = []
        self._stopped = False
        self._auth_failed = False
        self.connected = False
        self._push_error_kind = None
        self._push_error_code = None
        self._last_errors = {}
        self.touched: dict[str, set[str]] = {}
        self._last_received = dict.fromkeys(initial, clock())
        self._last_event: dict[str, float] = {}
        self._last_get: dict[str, float] = dict.fromkeys(initial, clock())
        self._last_http_get = dict(self._last_get)
        self._recovery_failures: dict[str, int] = {}
        self._recovery_due: dict[str, float] = {}
        self._resync_tasks: dict[str, asyncio.Task] = {}
        self._quiet_tasks: dict[str, asyncio.Task] = {}
        self._recovery_waiters: set[asyncio.Task] = set()
        self.schedulers = {
            device: InformationScheduler(
                lambda d=device: self._information(d),
                lambda d=device: self.recover(d),
                clock=clock,
                sleep=sleep,
                on_error=lambda error, d=device: self._error(d, error),
                fallback_recover=lambda d=device: self.recover(d, fallback=True),
            )
            for device in initial
        }

    def start(self) -> None:
        """Start once after platforms and coordinator listeners are ready."""
        if self._tasks or self._stopped:
            return
        for scheduler in self.schedulers.values():
            scheduler.start()
        self._tasks.append(asyncio.create_task(self._supervise()))
        self._tasks.append(asyncio.create_task(self._watchdog()))
        if self._discover is not None:
            self._tasks.append(asyncio.create_task(self._discover_loop()))

    def _error(self, device: str, error: Exception) -> float:
        self._last_errors[device] = {
            "age_origin": self._clock(),
            "type": type(error).__name__,
            "http_status": error.status
            if isinstance(error, ClientResponseError)
            else None,
        }
        if isinstance(error, ClientResponseError) and error.status == 401:
            if not self._auth_failed:
                self._auth_failed = True
                self._reauth()
            raise asyncio.CancelledError from None
        failures = max(
            self.schedulers[device].status.failures,
            self._recovery_failures.get(device, 0),
        )
        return min(300, 10 * 2 ** min(failures, 5)) * random.uniform(1, 1.2)

    async def _information(self, device: str) -> None:
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError

        push_at_start = self.schedulers[device].status.push_active

        async def send():
            async with self._locks(device), self._limit:
                if self._stopped or self._auth_failed:
                    raise asyncio.CancelledError
                if push_at_start and not self.schedulers[device].status.push_active:
                    return False
                if not push_at_start:
                    self._budget.mark_started()
                self.schedulers[device].status.last_information = self._clock()
                try:
                    await self._request(device)
                finally:
                    if not self.schedulers[device].status.push_active:
                        self._information_ready[device] = (
                            self._clock() + FALLBACK_READ_DELAY
                        )

        if push_at_start:
            return await send()
        return await self._budget.run(send)

    async def _read(self, installation: str, on_start=None) -> list[dict[str, Any]]:
        checkpoint = self.state.checkpoint()

        async def read():
            nonlocal checkpoint
            ready = max(
                (
                    deadline
                    for device, deadline in self._information_ready.items()
                    if self._installations.get(device) == installation
                ),
                default=0,
            )
            if ready > self._clock():
                await self._sleep(ready - self._clock())
            async with self._limit:
                if self._stopped or self._auth_failed:
                    raise asyncio.CancelledError
                self._budget.mark_started()
                checkpoint = self.state.checkpoint()
                if on_start is not None:
                    on_start()
                return await self._fetch(installation)

        devices = await self._budget.run(read)
        for device in list(self._information_ready):
            if self._installations.get(device) == installation:
                self._information_ready.pop(device, None)
        if not isinstance(devices, list):
            raise ValueError("Invalid recovery snapshot")
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError
        devices = [dict(d) for d in devices]
        for device in devices:
            if not device.get("id") and device.get("mac"):
                device["id"] = str(device["mac"]).strip().lower()
        for device in devices:
            key = str(device.get("id") or "")
            if key in self.schedulers:
                self._last_http_get[key] = self._clock()
        if checkpoint[0] != self.state.generation:
            raise ObsoleteSnapshot
        self.touched = {
            str(d.get("id")): {k for k, v in d.items() if v is not None}
            for d in devices
        }
        self.state.snapshot(devices, checkpoint)
        now = self._clock()
        for device in devices:
            key = str(device.get("id") or "")
            if key in self.schedulers:
                self._last_get[key] = self._last_received[key] = now
        self._publish(deepcopy(self.state.data), True)
        return devices

    async def recover(self, device: str, *, fallback=False) -> None:
        """Own recovery waiters even when started by an explicit HA refresh."""
        task = asyncio.current_task()
        if task is not None:
            self._recovery_waiters.add(task)
        try:
            await self._recover(device, fallback=fallback)
        finally:
            if task is not None:
                self._recovery_waiters.discard(task)

    async def _recover(self, device: str, *, fallback=False) -> None:
        """Coalesce reads only when started after this caller is ready."""
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError
        delay = self._recovery_due.get(device, 0) - self._clock()
        if delay > 0:
            await self._sleep(delay)
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError
        try:
            while True:
                try:
                    await self._pool.get(
                        self._installations[device],
                        min(
                            self._clock(),
                            self._information_ready.get(device, self._clock()),
                        )
                        if fallback
                        else self._clock(),
                    )
                    break
                except ObsoleteSnapshot:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._recovery_failures[device] = self._recovery_failures.get(device, 0) + 1
            self._recovery_due[device] = self._clock() + self._error(device, error)
            raise
        else:
            self._recovery_failures.pop(device, None)
            self._recovery_due.pop(device, None)

    async def refresh(self) -> dict[str, dict[str, Any]]:
        """Explicit coordinator recovery without starting another polling loop."""
        devices = list(self.schedulers)
        await asyncio.gather(*(self.recover(d) for d in devices))
        return deepcopy(self.state.data)

    async def event(self, device: str, patch: dict[str, Any], generation: int) -> None:
        """Publish decoded channel data in receive order."""
        if self._stopped or self._auth_failed or generation != self.state.generation:
            return
        if device not in self.schedulers or not isinstance(patch, dict):
            return
        if "id" in patch and str(patch["id"]) != device:
            return
        self.state.event(device, patch, generation)
        self.touched = {device: {k for k, v in patch.items() if v is not None}}
        self._last_event[device] = self._last_received[device] = self._clock()
        self._publish(deepcopy(self.state.data), True)

    async def subscription(self, device: str, active: bool) -> None:
        """Never block the socket reader waiting for a REST resynchronization."""
        if self._stopped or device not in self.schedulers:
            return
        if active:
            if not self.connected:
                _LOGGER.info("Cloud push connection active")
            self._push_error_kind = None
            self.connected = True
        old = self._resync_tasks.pop(device, None)
        if old:
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        if not active:
            await self.schedulers[device].set_push(False)
            return

        async def resync() -> None:
            while not self._stopped and not self._auth_failed:
                try:
                    await self.schedulers[device].set_push(True)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    delay = self._error(device, error)
                    await self._sleep(delay)

        self._resync_tasks[device] = asyncio.create_task(resync())

    async def _supervise(self) -> None:
        failures = 0
        while not self._stopped and not self._auth_failed:
            started = self._clock()
            if failures and self._discover is not None:
                if self._topology_task is None or self._topology_task.done():
                    self._topology_task = asyncio.create_task(self._discover_once())
            self.state.invalidate()
            try:
                await self._connect(self)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, ClientResponseError) and error.status == 401:
                    if not self._auth_failed:
                        self._auth_failed = True
                        self._reauth()
                    return
                self._push_error_code = (
                    error.code
                    if isinstance(error, PusherProtocolError)
                    else getattr(error, "status", None)
                )
                kind = type(error).__name__
                if kind != self._push_error_kind:
                    _LOGGER.warning("Cloud push connection interrupted (%s)", kind)
                    self._push_error_kind = kind
                if isinstance(error, PusherProtocolError) and error.code is not None:
                    if 4000 <= error.code <= 4099:
                        return
            finally:
                self.state.invalidate()
                self.connected = False
                for device in self.schedulers:
                    await self.subscription(device, False)
            failures = 0 if self._clock() - started >= 60 else failures + 1
            await self._sleep(min(60, 2 ** min(failures, 6)) * random.uniform(1, 1.2))

    async def _discover_once(self) -> None:
        async with self._discovery_lock:
            if self._stopped or self._auth_failed or self._discover is None:
                return
            if (
                self._last_discovery is not None
                and self._clock() - self._last_discovery < 60
            ):
                return
            self._last_discovery = self._clock()
            try:
                mapping = await self._discover()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, ClientResponseError) and error.status == 401:
                    if not self._auth_failed:
                        self._auth_failed = True
                        self._reauth()
                _LOGGER.warning(
                    "Cloud topology refresh failed (%s)", type(error).__name__
                )
                return
            if not self._stopped and mapping != self._installations:
                if self._on_topology_change is not None:
                    self._on_topology_change()

    async def _discover_loop(self) -> None:
        while not self._stopped and not self._auth_failed:
            await self._sleep(900)
            await self._discover_once()

    async def _watchdog(self) -> None:
        """Recover quiet channels independently of the local-only entity clock."""
        while not self._stopped and not self._auth_failed:
            await self._sleep(60)
            for device, scheduler in self.schedulers.items():
                if not scheduler.status.push_active:
                    continue
                if (
                    self._clock()
                    - self._last_event.get(device, self._last_received[device])
                    < 60
                ):
                    continue
                self.request_recovery(device)

    def request_recovery(self, device: str) -> None:
        """Coalesce independent quiet-channel and unconfirmed-write recovery."""
        if self._stopped or self._auth_failed or device not in self.schedulers:
            return
        existing = self._quiet_tasks.get(device)
        if existing is not None and not existing.done():
            return

        async def recover():
            try:
                await self.recover(device)
            except asyncio.CancelledError:
                raise
            except Exception:
                # recover() records the device backoff and handles native reauth.
                return

        self._quiet_tasks[device] = asyncio.create_task(recover())

    def maintenance(self) -> None:
        """Notify local listeners without advancing reception timestamps."""
        if not self._stopped:
            self._publish(deepcopy(self.state.data), False)

    def _reported_age(self, device: str) -> float | None:
        value = self.state.data[device].get("update_date")
        if not isinstance(value, str):
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        return max(0.0, (dt_util.utcnow() - dt_util.as_utc(parsed)).total_seconds())

    def diagnostics(self) -> dict[str, Any]:
        """Return only durations, counters and transport health."""
        now = self._clock()
        return {
            "pusher_connected": self.connected,
            "pusher_error_code": self._push_error_code,
            "authentication_required": self._auth_failed,
            "automatic_budget": self._budget.diagnostics(),
            "devices": {
                f"device_{i}": {
                    "mode": (
                        "stopped"
                        if self._stopped
                        else "auth_required"
                        if self._auth_failed
                        else "resyncing"
                        if d in self._resync_tasks and not self._resync_tasks[d].done()
                        else "push"
                        if s.status.push_active
                        else "rest_fallback"
                    ),
                    "last_event_age": now - self._last_event[d]
                    if d in self._last_event
                    else None,
                    "last_get_age": now - self._last_http_get[d]
                    if d in self._last_http_get
                    else None,
                    "last_applied_get_age": now - self._last_get[d]
                    if d in self._last_get
                    else None,
                    "last_reception_age": now - self._last_received[d],
                    "state_age": self._reported_age(d),
                    "state_age_source": "reported_update_date",
                    "physical_freshness_verified": False,
                    "reception_stale": now - self._last_received[d] >= 60,
                    "last_information_age": now - s.status.last_information
                    if s.status.last_information is not None
                    else None,
                    "information_result": s.status.result,
                    "failures": max(
                        s.status.failures, self._recovery_failures.get(d, 0)
                    ),
                    "last_error": {
                        k: v
                        for k, v in self._last_errors.get(d, {}).items()
                        if k != "age_origin"
                    },
                    "last_error_age": now - self._last_errors[d]["age_origin"]
                    if d in self._last_errors
                    else None,
                    "next_cycle_in": max(0, (s.status.next_attempt or now) - now),
                    "backoff_remaining": max(
                        0,
                        max(
                            s.status.next_attempt or now, self._recovery_due.get(d, now)
                        )
                        - now,
                    )
                    if s.status.failures or d in self._recovery_due
                    else 0.0,
                }
                for i, (d, s) in enumerate(self.schedulers.items(), 1)
            },
        }

    async def close(self) -> None:
        """Fence callbacks, cancel tasks and await all owned I/O."""
        self._stopped = True
        self.state.invalidate()
        self._budget.close()
        tasks = (
            self._tasks
            + list(self._resync_tasks.values())
            + list(self._quiet_tasks.values())
        )
        if self._topology_task is not None:
            tasks.append(self._topology_task)
        tasks = set(tasks) | self._recovery_waiters
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(s.close() for s in self.schedulers.values()))
        await self._pool.close()
        self._tasks.clear()
        self._resync_tasks.clear()
        self._quiet_tasks.clear()
