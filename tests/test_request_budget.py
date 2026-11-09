"""Synthetic time traces for the entry-wide automatic request budget."""

import asyncio

import pytest

from custom_components.airzoneclouddaikin.request_budget import RequestBudget
from custom_components.airzoneclouddaikin.sync_scheduler import InformationScheduler


async def test_alternating_fallback_trace():
    now, trace = [0.0], []

    async def sleep(seconds):
        now[0] += seconds
        await asyncio.sleep(0)

    budget = RequestBudget(lambda: now[0], sleep)

    async def information():
        async def operation():
            trace.append((now[0], "information"))
            if len(trace) == 5:
                raise asyncio.CancelledError

        await budget.run(operation)

    async def get():
        async def operation():
            trace.append((now[0], "get"))

        await budget.run(operation)

    scheduler = InformationScheduler(
        information, get, clock=lambda: now[0], sleep=sleep, on_error=lambda _: 10
    )
    with pytest.raises(asyncio.CancelledError):
        await scheduler._run()
    assert trace == [
        (0, "information"),
        (10, "get"),
        (20, "information"),
        (30, "get"),
        (40, "information"),
    ]


async def test_multiple_callers_share_budget_and_never_overlap():
    now, starts, running = [0.0], [], [0]

    async def sleep(seconds):
        now[0] += seconds
        await asyncio.sleep(0)

    budget = RequestBudget(lambda: now[0], sleep)

    async def operation():
        starts.append(now[0])
        running[0] += 1
        assert running[0] == 1
        await asyncio.sleep(0)
        running[0] -= 1

    await asyncio.gather(*(budget.run(operation) for _ in range(6)))
    assert starts == [0, 10, 20, 30, 40, 50]


async def test_failed_rest_backs_off_from_failure_not_start():
    now, starts = [0.0], []

    async def sleep(seconds):
        now[0] += seconds

    budget = RequestBudget(lambda: now[0], sleep)

    async def fail():
        starts.append(now[0])
        now[0] += 35
        raise TimeoutError

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await budget.run(fail)
    assert starts == [0, 45]


async def test_delayed_actual_http_start_updates_budget_deadline():
    now, starts = [0.0], []

    async def sleep(seconds):
        now[0] += seconds

    budget = RequestBudget(lambda: now[0], sleep)

    async def delayed():
        now[0] += 25
        budget.mark_started()
        starts.append(now[0])

    async def immediate():
        starts.append(now[0])

    await budget.run(delayed)
    await budget.run(immediate)
    assert starts == [25, 35]


async def test_hidden_api_cooldown_cannot_release_a_burst():
    now, starts = [0.0], []

    async def sleep(seconds):
        now[0] += seconds

    budget = RequestBudget(lambda: now[0], sleep)

    async def delayed_send():
        # The API's shared Retry-After wait happens inside the operation.
        now[0] += 120
        starts.append(now[0])
        now[0] += 1

    async def next_send():
        starts.append(now[0])

    await budget.run(delayed_send)
    await budget.run(next_send)
    assert starts == [120, 131]
