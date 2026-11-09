"""Single-owner information cycles, independent of entity count and transport."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import ClientError, ClientResponseError

INFORMATION_INTERVAL = 10.0
FALLBACK_READ_DELAY = 10.0


@dataclass
class CycleStatus:
    """Non-identifying cycle diagnostics; monotonic timestamps only."""

    push_active: bool = False
    last_information: float | None = None
    last_success: float | None = None
    failures: int = 0
    next_attempt: float | None = None
    result: str = "idle"


class InformationScheduler:
    """Run one cancellable information cycle; the caller owns HTTP retries."""

    def __init__(
        self,
        request: Callable[[], Awaitable[None]],
        recover: Callable[[], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_error: Callable[[Exception], float],
        fallback_recover=None,
    ) -> None:
        self.status = CycleStatus()
        self._request = request
        self._recover = recover
        self._fallback_recover = fallback_recover or recover
        self._information_due = 0.0
        self._get_due = 0.0
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error
        self._task: asyncio.Task[None] | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    def start(self) -> None:
        """Idempotently start the sole cycle owner."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def set_push(self, active: bool) -> None:
        """Fence deferred fallback reads and resynchronize before enabling push."""
        self._generation += 1
        generation = self._generation
        self.status.push_active = False
        if active:
            async with self._lock:
                await self._recover()
                if generation == self._generation:
                    self.status.push_active = True

    async def close(self) -> None:
        """Cancel and await all owned work, including delayed GETs."""
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def cycle(self) -> None:
        """Execute one cycle without overlapping a recovery or another cycle."""
        async with self._lock:
            generation = self._generation
            self.status.last_information = self._clock()
            rejected = None
            try:
                result = None
                if self._clock() >= self._information_due:
                    result = await self._request()
                    self._information_due = 0
                    self.status.failures = 0
                if result is False:
                    self.status.result = "deferred"
                    return
            except (ClientError, TimeoutError, OSError) as error:
                status = (
                    error.status if isinstance(error, ClientResponseError) else None
                )
                if self.status.push_active or status in {401, 429}:
                    raise
                if status in {422, 423}:
                    rejected = error
                elif status is None or status >= 500:
                    self.status.failures += 1
                    self._information_due = self._clock() + self._on_error(error)
                    self.status.result = "information_error"
                else:
                    raise
            self.status.result = (
                "information_backoff"
                if self._information_due
                else "accepted"
                if rejected is None
                else "rejected"
            )
            if not self.status.push_active:
                await self._sleep(FALLBACK_READ_DELAY)
                if (
                    generation == self._generation
                    and not self.status.push_active
                    and self._clock() >= self._get_due
                ):
                    try:
                        await self._fallback_recover()
                    except (ClientError, TimeoutError, OSError) as error:
                        if isinstance(error, ClientResponseError) and error.status in {
                            401,
                            429,
                        }:
                            raise
                        self._get_due = self._clock() + self._on_error(error)
                    else:
                        self._get_due = 0
            self.status.last_success = self._clock()
            if rejected is not None:
                raise rejected

    async def _run(self) -> None:
        while True:
            started = self._clock()
            fallback = not self.status.push_active
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # policy classifies errors without logging them
                self.status.failures += 1
                self.status.result = "error"
                delay = max(INFORMATION_INTERVAL, self._on_error(error))
                started = self._clock()
            else:
                delay = INFORMATION_INTERVAL * (2 if fallback else 1)
            # Missed slots are skipped, never replayed or queued.
            deadline = started + delay
            now = self._clock()
            if deadline < now:
                deadline = now + INFORMATION_INTERVAL
            self.status.next_attempt = deadline
            await self._sleep(max(0.0, deadline - now))
