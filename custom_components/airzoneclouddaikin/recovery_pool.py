"""Coalesce recovery reads while respecting each caller's readiness time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class RecoveryPool:
    """Own at most one GET per installation; never reuse a too-early read."""

    def __init__(
        self,
        fetch: Callable[[str], Awaitable[Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._pending: dict[str, tuple[float, asyncio.Task[Any]]] = {}
        self._closed = False

    async def get(self, installation: str, ready_at: float) -> Any:
        """Share an eligible read, or wait for an older read before starting one."""
        if ready_at > self._clock():
            raise ValueError("Recovery requested before its readiness deadline")
        while not self._closed:
            pending = self._pending.get(installation)
            if pending is None:
                task = asyncio.create_task(self._fetch(installation))
                pending = (self._clock(), task)
                self._pending[installation] = pending
            started, task = pending
            try:
                result = await asyncio.shield(task)
            finally:
                if task.done() and self._pending.get(installation) is pending:
                    self._pending.pop(installation)
            if started >= ready_at:
                return result
        raise asyncio.CancelledError

    async def close(self) -> None:
        """Cancel and await reads even when their original waiters disappeared."""
        self._closed = True
        tasks = [task for _, task in self._pending.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
