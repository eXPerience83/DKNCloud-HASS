"""Coalesce recovery reads while respecting each caller's readiness time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import count
from typing import Any

from aiohttp import ClientResponseError

type ReadStart = tuple[float, int]


@dataclass
class _Read:
    started: ReadStart | None = None
    task: asyncio.Task | None = None


class RecoveryPool:
    """Own at most one GET per installation; never reuse a too-early read."""

    def __init__(
        self,
        fetch: Callable[[str], Awaitable[Any]],
        clock: Callable[[], float] = time.monotonic,
        timed_fetch=None,
    ) -> None:
        self._fetch = fetch
        self._timed_fetch = timed_fetch
        self._clock = clock
        self._order = count()
        self._pending: dict[str, _Read] = {}
        self._closed = False

    def stamp(self) -> ReadStart:
        """Order demands and GET starts even when the monotonic clock ties."""
        return self._clock(), next(self._order)

    async def get(
        self,
        installation: str,
        ready_at: float | None,
        *,
        latest_demand: Callable[[], ReadStart | None] | None = None,
    ) -> Any:
        """Share an eligible read, checking the latest demand after publication."""
        if ready_at is not None and ready_at > self._clock():
            raise ValueError("Recovery requested before its readiness deadline")
        while not self._closed:
            pending = self._pending.get(installation)
            if pending is None:
                pending = _Read()
                self._pending[installation] = pending

                async def read(record=pending):
                    # Let concurrent ready callers join before starting I/O.
                    await asyncio.sleep(0)

                    def started():
                        record.started = self.stamp()

                    if self._timed_fetch is not None:
                        return await self._timed_fetch(installation, started)
                    started()
                    return await self._fetch(installation)

                pending.task = asyncio.create_task(read())

                def forget_finished(
                    completed: asyncio.Task, record: _Read = pending
                ) -> None:
                    # A cancelled last waiter leaves the shielded GET running.
                    # Retire its record after completion even if nobody awaits
                    # it again; never remove a newer read for this installation.
                    if self._pending.get(installation) is record:
                        self._pending.pop(installation, None)
                    # Observe failures when every waiter was cancelled to
                    # avoid unhandled background task exceptions.
                    if not completed.cancelled():
                        completed.exception()

                pending.task.add_done_callback(forget_finished)
            task = pending.task
            assert task is not None
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, ClientResponseError) and error.status == 401:
                    raise
                # A background worker with pending demands must record
                # backoff before attempting any more reads for this device.
                if (
                    latest_demand is None
                    and ready_at is not None
                    and pending.started is not None
                    and pending.started[0] < ready_at
                ):
                    continue
                raise
            finally:
                if task.done() and self._pending.get(installation) is pending:
                    self._pending.pop(installation)
            demand = latest_demand() if latest_demand is not None else None
            if (
                pending.started is not None
                and (ready_at is None or pending.started[0] >= ready_at)
                and (demand is None or pending.started >= demand)
            ):
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
