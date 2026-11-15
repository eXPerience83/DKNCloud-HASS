"""Transport admission regressions for the INFO that escaped the real trial."""

import asyncio
import time

import pytest

from scripts.lab_policy import ProtectedSession, ReadOnlyPolicy
from scripts.protected_lab import GuardedContext, LaboratoryGateway, LabSupervisor
from scripts.trial_timeline import TrialTimeline


class Reply:
    status = 204

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def gateway():
    return LaboratoryGateway(
        ProtectedSession(None, ReadOnlyPolicy({"device"}, {"site"}, "synthetic")),
        LabSupervisor(spacing=0),
        timeline=TrialTimeline(),
    )


async def context(gw, operation, calls, *, entered=None, release=None):
    def factory():
        calls.append(operation)
        return Reply()

    async with GuardedContext(gw, factory, "state", operation=operation):
        if entered:
            entered.set()
        if release:
            await release.wait()


@pytest.mark.parametrize("operation", ["info", "get"])
async def test_read_already_waiting_for_budget_cannot_escape_pause(operation):
    gw = gateway()
    gw.supervisor._next_state = time.monotonic() + 0.03
    calls = []
    # Construct and enqueue the context BEFORE the experiment closes the gate.
    read = asyncio.create_task(context(gw, operation, calls))
    await asyncio.sleep(0)
    await gw.read_window.pause(operation)
    await context(gw, "control", calls)
    assert calls == ["control"]
    assert not read.done()
    assert gw.supervisor.counts["state"] == 1
    gw.read_window.resume(operation)
    await read
    assert calls == ["control", operation]


async def test_single_get_permit_with_queued_reads_and_independent_info():
    gw = gateway()
    await gw.read_window.pause()
    calls = []
    tasks = [asyncio.create_task(context(gw, "get", calls)) for _ in range(3)]
    tasks.append(asyncio.create_task(context(gw, "info", calls)))
    await asyncio.sleep(0)
    gw.read_window.allow_once("get")
    while not calls:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == ["get"] and gw.read_window.paused("get")
    gw.read_window.resume("info")
    await tasks[-1]
    assert calls == ["get", "info"]
    assert sum(t.done() for t in tasks[:-1]) == 1
    gw.read_window.resume("get")
    await asyncio.gather(*tasks)
    assert calls.count("get") == 3


async def test_pause_drains_delegated_response_and_cancellation_releases_lease():
    gw = gateway()
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    active = asyncio.create_task(
        context(gw, "get", calls, entered=entered, release=release)
    )
    await entered.wait()
    paused = asyncio.create_task(gw.read_window.pause("get"))
    await asyncio.sleep(0)
    assert not paused.done()
    active.cancel()
    await asyncio.gather(active, return_exceptions=True)
    await paused
    assert gw.read_window._active["get"] == 0
    assert gw.read_window._idle["get"].is_set()
    # Cancel a queued read; it must consume neither budget nor a permit.
    waiting = asyncio.create_task(context(gw, "get", calls))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert gw.supervisor.counts["state"] == 1
