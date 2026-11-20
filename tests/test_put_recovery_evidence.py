"""Synthetic ambiguous outcomes and raw readback; never a real timeout."""

import asyncio

import pytest
from aiohttp import ClientResponseError

from custom_components.airzoneclouddaikin import airzone_api
from scripts.lab_tls_backend import SyntheticBackend
from scripts.protected_lab import LabSupervisor, create_gateway
from scripts.put_trial import BoundedPutPolicy, PutPlan
from scripts.read_experiment import snapshot_confirmed
from scripts.setpoint_trial import SingleUseTrialJournal
from scripts.trial_timeline import TrialTimeline


@pytest.mark.parametrize("outcome", ["accepted-response-lost", "never-arrived"])
async def test_unknown_put_outcomes_consume_once_and_forbid_restore(
    tmp_path, socket_enabled, monkeypatch, outcome
):
    backend = await SyntheticBackend(tmp_path).start()
    backend.trial_put_field = "min_temp_unoccupied"
    backend.trial_push_on_info = True
    current = {
        "id": "device",
        "power": "0",
        "mode": "1",
        "scenary": "occupied",
        "min_temp_unoccupied": 18,
    }
    plan = PutPlan.from_device(
        current, device_id="device", installation_id="site", field="min_temp_unoccupied"
    )
    folder = tmp_path / "journal"
    folder.mkdir(mode=0o700)
    journal = SingleUseTrialJournal(folder, nonce="recovery-test" * 4, plan=plan)
    policy = BoundedPutPolicy(
        plan, journal, lambda: current, "synthetic", deadline=10**12
    )
    gw = await create_gateway(
        policy, LabSupervisor(spacing=0), connector=backend.connector()
    )
    api = airzone_api.AirzoneAPI("synthetic@example.invalid", gw, token="fake")
    if outcome == "accepted-response-lost":
        backend.put_response_wait = asyncio.Event()
        monkeypatch.setattr(airzone_api, "REQUEST_TIMEOUT", 0.05)
    else:
        # Failure after durable admission but before any server request.
        def lost_connection(*args, **kwargs):
            raise TimeoutError("Synthetic loss before delivery")

        monkeypatch.setattr(gw.protected.session, "request", lost_connection)
    policy.arm_change()
    try:
        with gw.enforce():
            with pytest.raises(TimeoutError):
                await api.put_device_fields("device", {"min_temp_unoccupied": 19})
        assert backend.min_temp_unoccupied == (
            19 if outcome == "accepted-response-lost" else 18
        )
        assert backend.controls == (1 if outcome == "accepted-response-lost" else 0)
        assert policy._revoked
        assert journal.path.read_text().count('"kind":"attempt"') == 1
        with pytest.raises(PermissionError):
            policy.arm_restoration(confirmed_by_snapshot=True, owner_approved=True)
        assert not snapshot_confirmed(
            [], plan.field, plan.modified, state_value=plan.modified
        )
    finally:
        if backend.put_response_wait:
            backend.put_response_wait.set()
        await gw.protected.session.close()
        await backend.close()
        journal.close()


def test_old_get_cannot_confirm_an_optimistic_or_retained_push_value():
    field = "min_temp_unoccupied"
    records = [
        {"kind": "ha_optimistic", "field": field, "value_c": 17},
        {"kind": "get_received", "field": field, "value_c": 16},
        {"kind": "get_applied", "field": field, "value_c": 17},
    ]
    assert not snapshot_confirmed(records, field, 17, state_value=17)
    records[1]["value_c"] = 17
    records.pop()  # Matching raw value superseded by Pusher is not GET-applied.
    assert snapshot_confirmed(records, field, 17, state_value=17)
    assert not snapshot_confirmed(records, field, 17, state_value=16)
    records.append({"kind": "get_applied", "field": field, "value_c": 17})
    assert snapshot_confirmed(records, field, 17, state_value=17)


@pytest.mark.parametrize("info_fails", [False, True])
async def test_info_pusher_get_and_no_push_failed_info_get_fallback(
    tmp_path, socket_enabled, info_fails
):
    from scripts.lab_policy import ReadOnlyPolicy

    backend = await SyntheticBackend(tmp_path).start()
    backend.min_temp_unoccupied = 19
    backend.trial_push_on_info = True
    backend.pending_put_patch = {"id": "device", "min_temp_unoccupied": 19}
    backend.info_status = 503 if info_fails else 201
    if info_fails:
        backend.pending_put_patch = None
    timeline = TrialTimeline()
    gw = await create_gateway(
        ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True),
        LabSupervisor(spacing=0),
        connector=backend.connector(),
    )
    gw.timeline = timeline
    api = airzone_api.AirzoneAPI("synthetic@example.invalid", gw, token="fake")
    try:
        with gw.enforce():
            if info_fails:
                with pytest.raises(ClientResponseError):
                    await api.request_information("device")
            else:
                await api.request_information("device")
                assert backend.events == 1  # Publisher invoked only by INFO.
            snapshot = await api.fetch_devices("site")
        assert snapshot[0]["min_temp_unoccupied"] == 19
        assert backend.controls == 0
        assert backend.operations["info"] == backend.operations["get"] == 1
        assert backend.events == (0 if info_fails else 1)
        assert not snapshot_confirmed(
            timeline.export(), "min_temp_unoccupied", 19, state_value=18
        )
        # Transport receipt alone is not coordinator application/confirmation.
    finally:
        await gw.protected.session.close()
        await backend.close()
