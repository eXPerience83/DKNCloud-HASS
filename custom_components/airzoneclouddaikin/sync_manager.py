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

from .pusher_client import PusherProtocolError
from .recovery_pool import RecoveryPool
from .sync_scheduler import InformationScheduler
from .sync_state import SyncState

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self.state = SyncState()
        self.state.snapshot(list(initial.values()), self.state.checkpoint())
        self._installations = dict(installations)
        self._request, self._fetch = request, fetch
        self._publish, self._reauth, self._locks = publish, reauth, locks
        self._connect, self._clock = connect, clock
        self._limit = asyncio.Semaphore(2)
        self._pool = RecoveryPool(self._read, clock)
        self._tasks: list[asyncio.Task] = []
        self._stopped = False
        self._auth_failed = False
        self.connected = False
        self.touched: dict[str, set[str]] = {}
        self._last_received = dict.fromkeys(initial, clock())
        self._last_event: dict[str, float] = {}
        self._last_get: dict[str, float] = dict.fromkeys(initial, clock())
        self._recovery_failures: dict[str, int] = {}
        self._recovery_due: dict[str, float] = {}
        self._resync_tasks: dict[str, asyncio.Task] = {}
        self.schedulers = {
            device: InformationScheduler(
                lambda d=device: self._information(d),
                lambda d=device: self.recover(d),
                clock=clock,
                on_error=lambda error, d=device: self._error(d, error),
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

    def _error(self, device: str, error: Exception) -> float:
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
        async with self._locks(device), self._limit:
            await self._request(device)

    async def _read(self, installation: str) -> list[dict[str, Any]]:
        checkpoint = self.state.checkpoint()
        async with self._limit:
            devices = await self._fetch(installation)
        if not isinstance(devices, list):
            raise ValueError("Invalid recovery snapshot")
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError
        if checkpoint[0] != self.state.generation:
            return devices
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

    async def recover(self, device: str) -> None:
        """Coalesce reads only when started after this caller is ready."""
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError
        delay = self._recovery_due.get(device, 0) - self._clock()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._stopped or self._auth_failed:
            raise asyncio.CancelledError
        try:
            await self._pool.get(self._installations[device], self._clock())
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
                    await asyncio.sleep(delay)

        self._resync_tasks[device] = asyncio.create_task(resync())

    async def _supervise(self) -> None:
        failures = 0
        while not self._stopped and not self._auth_failed:
            started = self._clock()
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
                _LOGGER.warning(
                    "Cloud push connection interrupted (%s)", type(error).__name__
                )
                if isinstance(error, PusherProtocolError) and error.code is not None:
                    if 4000 <= error.code <= 4099:
                        return
            finally:
                self.state.invalidate()
                self.connected = False
                for device in self.schedulers:
                    await self.subscription(device, False)
            failures = 0 if self._clock() - started >= 60 else failures + 1
            await asyncio.sleep(min(60, 2 ** min(failures, 6)) * random.uniform(1, 1.2))

    async def _watchdog(self) -> None:
        """Recover quiet channels independently of the local-only entity clock."""
        while not self._stopped and not self._auth_failed:
            await asyncio.sleep(60)
            for device, scheduler in self.schedulers.items():
                if not scheduler.status.push_active:
                    continue
                if (
                    self._clock()
                    - self._last_event.get(device, self._last_received[device])
                    < 60
                ):
                    continue
                try:
                    await self.recover(device)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._error(device, error)

    def maintenance(self) -> None:
        """Notify local listeners without advancing reception timestamps."""
        if not self._stopped:
            self._publish(deepcopy(self.state.data), False)

    def diagnostics(self) -> dict[str, Any]:
        """Return only durations, counters and transport health."""
        now = self._clock()
        return {
            "pusher_connected": self.connected,
            "authentication_required": self._auth_failed,
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
                    "last_get_age": now - self._last_get[d]
                    if d in self._last_get
                    else None,
                    "state_age": now - self._last_received[d],
                    "stale": now - self._last_received[d] >= 60,
                    "last_information_age": now - s.status.last_information
                    if s.status.last_information is not None
                    else None,
                    "information_result": s.status.result,
                    "failures": max(
                        s.status.failures, self._recovery_failures.get(d, 0)
                    ),
                    "backoff_remaining": max(
                        0,
                        max(
                            s.status.next_attempt or now, self._recovery_due.get(d, now)
                        )
                        - now,
                    ),
                }
                for i, (d, s) in enumerate(self.schedulers.items(), 1)
            },
        }

    async def close(self) -> None:
        """Fence callbacks, cancel tasks and await all owned I/O."""
        self._stopped = True
        self.state.invalidate()
        tasks = self._tasks + list(self._resync_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(s.close() for s in self.schedulers.values()))
        await self._pool.close()
        self._tasks.clear()
        self._resync_tasks.clear()
