"""Entry-wide budget for automatic fallback and recovery HTTP operations."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import ClientError, ClientResponseError


class RequestBudget:
    """Serialize operations with ten seconds between actual starts, without ticks."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep
    ):
        self._clock, self._sleep = clock, sleep
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._failures = 0
        self._closed = False

    async def run(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            while self._next > self._clock():
                await self._sleep(self._next - self._clock())
            if self._closed:
                raise asyncio.CancelledError
            started = self._clock()
            self._next = started + 10
            try:
                result = await operation()
            except (ClientError, TimeoutError, OSError) as error:
                transient = (
                    not isinstance(error, ClientResponseError)
                    or error.status == 429
                    or error.status >= 500
                )
                if transient:
                    self._failures += 1
                    self._next = max(
                        self._next,
                        self._clock() + min(300, 10 * 2 ** min(self._failures - 1, 5)),
                    )
                raise
            else:
                self._failures = 0
                return result

    def mark_started(self) -> None:
        """Account for an HTTP start delayed by device locks or batch readiness."""
        self._next = max(self._next, self._clock() + 10)

    def diagnostics(self):
        wait = max(0.0, self._next - self._clock())
        return {
            "minimum_interval": 10.0,
            "wait_remaining": wait,
            "failures": self._failures,
            "backoff_remaining": wait if self._failures else 0.0,
        }

    def close(self):
        """Fence queued operations; the owner cancels and awaits their tasks."""
        self._closed = True
