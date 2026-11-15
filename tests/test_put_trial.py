"""Scoped PUT controls and an isolated LAB read window; no real service access."""

import asyncio

import pytest

from scripts.lab_policy import ProtectedSession
from scripts.lab_tls_backend import SyntheticBackend
from scripts.protected_lab import (
    LaboratoryGateway,
    LabStopped,
    LabSupervisor,
    create_gateway,
)
from scripts.put_trial import BoundedPutPolicy, PutPlan, ReadWindow
from scripts.setpoint_trial import SingleUseTrialJournal


class Reply:
    status = 204

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Raw:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Reply()


@pytest.mark.parametrize("field", ["scenary", "min_temp_unoccupied"])
async def test_only_two_frozen_put_attempts_with_no_other_controls(tmp_path, field):
    raw = device()
    plan = PutPlan.from_device(
        raw, device_id="device", installation_id="site", field=field
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    book = SingleUseTrialJournal(private, nonce="b" * 40, plan=plan)
    policy = BoundedPutPolicy(plan, book, lambda: raw, "synthetic", deadline=10**12)
    transport = Raw()
    gateway = LaboratoryGateway(
        ProtectedSession(transport, policy), LabSupervisor(spacing=0)
    )
    params = {
        "user_email": "synthetic@example.invalid",
        "user_token": "fake",
        "format": "json",
    }
    try:
        policy.arm_change()
        with pytest.raises(PermissionError):
            policy.validate(
                "PUT",
                "https://dkn.airzonecloud.com/devices/other",
                params=params,
                json=plan.exact_payload(restoring=False),
            )
        with pytest.raises(PermissionError):
            policy.validate(
                "PUT",
                "https://dkn.airzonecloud.com/devices/device",
                params=params,
                json={"sleep_time": 30},
            )
        async with gateway.request(
            "PUT",
            "https://dkn.airzonecloud.com/devices/device",
            params=params,
            json=plan.exact_payload(restoring=False),
        ):
            pass
        raw[field] = plan.modified
        policy.arm_restoration(confirmed_by_snapshot=True, owner_approved=True)
        async with gateway.request(
            "PUT",
            "https://dkn.airzonecloud.com/devices/device",
            params=params,
            json=plan.exact_payload(restoring=True),
        ):
            pass
        with pytest.raises(LabStopped):
            gateway.request(
                "PUT",
                "https://dkn.airzonecloud.com/devices/device",
                params=params,
                json=plan.exact_payload(restoring=True),
            )
        assert len(transport.calls) == 2
        assert book.path.read_text().count('"kind":"attempt"') == 2
    finally:
        book.close()


def device():
    return {
        "id": "device",
        "power": "1",
        "scenary": "occupied",
        "mode": "1",
        "min_temp_unoccupied": 18,
    }


@pytest.mark.parametrize("field", ["scenary", "min_temp_unoccupied"])
def test_put_plan_requires_home_and_preserves_exact_restore(field):
    raw = device()
    plan = PutPlan.from_device(
        raw, device_id="device", installation_id="site", field=field
    )
    assert plan.modified == ("vacant" if field == "scenary" else 19)
    raw[field] = plan.modified
    plan.check_device(raw, restoring=True)
    raw["power"] = "0"
    with pytest.raises(PermissionError):
        plan.check_device(raw, restoring=True)


@pytest.mark.parametrize(
    "updates", [{"scenary": "sleep"}, {"power": "0"}, {"min_temp_unoccupied": 23}]
)
def test_inappropriate_put_state_denied(updates):
    raw = device() | updates
    with pytest.raises(ValueError):
        PutPlan.from_device(
            raw, device_id="device", installation_id="site", field="min_temp_unoccupied"
        )


async def test_read_window_drains_pending_reads_and_releases_waiters():
    window = ReadWindow()
    await window.enter()
    pause = asyncio.create_task(window.pause())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(window.enter())
    await asyncio.sleep(0)
    assert not pause.done() and not waiter.done()
    window.leave()
    await pause
    assert not waiter.done()
    window.resume()
    await waiter
    window.leave()
    assert window.active == 0 and window.idle.is_set()


@pytest.mark.parametrize("failure", ["429", "timeout", "cancel"])
async def test_authentic_api_put_never_replays_uncertain_or_rejected_attempt(
    tmp_path, socket_enabled, monkeypatch, failure
):
    from custom_components.airzoneclouddaikin import airzone_api

    backend = await SyntheticBackend(tmp_path).start()
    raw = device()
    plan = PutPlan.from_device(
        raw, device_id="device", installation_id="site", field="scenary"
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    book = SingleUseTrialJournal(private, nonce="a" * 40, plan=plan)
    policy = BoundedPutPolicy(plan, book, lambda: raw, "synthetic", deadline=10**12)
    supervisor = LabSupervisor(duration=120, spacing=0)
    gateway = await create_gateway(policy, supervisor, connector=backend.connector())
    api = airzone_api.AirzoneAPI("synthetic@example.invalid", gateway, token="fake")
    if failure == "429":
        backend.status = 429
    else:
        backend.wait = asyncio.Event()
        backend.wait_path = "/devices/device"
    if failure == "timeout":
        monkeypatch.setattr(airzone_api, "REQUEST_TIMEOUT", 0.05)
    policy.arm_change()
    try:
        with gateway.enforce():
            task = asyncio.create_task(
                api.put_device_fields("device", plan.exact_payload(restoring=False))
            )
            while backend.calls == 0:
                await asyncio.sleep(0)
            if failure == "cancel":
                task.cancel()
            with pytest.raises((LabStopped, TimeoutError, asyncio.CancelledError)):
                await task
        assert backend.calls == 1
        assert policy._revoked
        assert book.path.read_text().count('"kind":"attempt"') == 1
        with pytest.raises(PermissionError):
            policy.arm_restoration(confirmed_by_snapshot=True, owner_approved=True)
    finally:
        if backend.wait:
            backend.wait.set()
        await gateway.protected.session.close()
        await backend.close()
        book.close()
