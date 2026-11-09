"""Coalesce recovery reads while respecting each caller's readiness time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class _Read:
    started: float | None = None
    task: asyncio.Task | None = None


class RecoveryPool:
    """Own at most one GET per installation; never reuse a too-early read."""

    def __init__(
        self,
        fetch: Callable[[str], Awaitable[Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._pending: dict[str, _Read] = {}
        self._closed = False

    async def get(self, installation: str, ready_at: float) -> Any:
        """Share an eligible read, or wait for an older read before starting one."""
        if ready_at > self._clock():
            raise ValueError("Recovery requested before its readiness deadline")
        while not self._closed:
            pending = self._pending.get(installation)
            if pending is None:
                pending = _Read()
                self._pending[installation] = pending

                async def read(record=pending):
                    # Let concurrent ready callers join before starting I/O.
                    await asyncio.sleep(0)
                    record.started = self._clock()
                    return await self._fetch(installation)

                pending.task = asyncio.create_task(read())
            task = pending.task
            assert task is not None
            try:
                result = await asyncio.shield(task)
            finally:
                if task.done() and self._pending.get(installation) is pending:
                    self._pending.pop(installation)
            if pending.started is not None and pending.started >= ready_at:
                return result
        raise asyncio.CancelledError

    async def close(self) -> None:
        """Cancel and await reads even when their original waiters disappeared."""
        self._closed = True
        tasks = [
            record.task for record in self._pending.values() if record.task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
