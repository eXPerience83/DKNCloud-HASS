"""End-to-end synthetic HTTPS transport admission with the existing LAB gateway."""

import json

import pytest

from scripts.lab_policy import create_protected_session
from scripts.lab_tls_backend import SyntheticBackend
from scripts.protected_lab import LaboratoryGateway, LabStopped, LabSupervisor
from scripts.setpoint_trial import (
    BoundedSetpointPolicy,
    SetpointPlan,
    SingleUseTrialJournal,
)
from scripts.trial_timeline import TrialTimeline

ENDPOINT = "https://dkn.airzonecloud.com/events"


@pytest.mark.asyncio
async def test_synthetic_tls_backend_accepts_exactly_two_controls_and_records_info(
    tmp_path,
    socket_enabled,
):
    backend = await SyntheticBackend(tmp_path).start()
    backend.trial_controls = True
    original = {
        "id": "device",
        "power": "1",
        "scenary": "occupied",
        "mode": "1",
        "cold_consign": "24",
        "min_limit_cold": "16.0",
        "max_limit_cold": "32.0",
    }
    frozen = SetpointPlan.from_device(
        original, device_id="device", installation_id="site"
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    journal = SingleUseTrialJournal(private, nonce="trial-" + "b" * 40, plan=frozen)
    policy = BoundedSetpointPolicy(
        frozen, journal, lambda: original, "synthetic", deadline=10**12
    )
    protected = await create_protected_session(
        policy, connector=backend.connector(trusted=True)
    )
    timeline = TrialTimeline()
    gateway = LaboratoryGateway(
        protected,
        LabSupervisor(duration=120, spacing=0, auth_limit=2, socket_limit=1),
        timeline=timeline,
    )
    params = {"user_email": "synthetic@example.invalid", "user_token": "fake"}
    info = {
        "event": {
            "cgi": "infomaquina",
            "device_id": "device",
            "option": "",
            "value": "",
        }
    }

    try:
        with gateway.enforce():
            async with gateway.request(
                "POST", ENDPOINT, params=params, json=info
            ) as response:
                assert response.status == 201
            policy.arm_change()
            async with gateway.request(
                "POST",
                ENDPOINT,
                params=params,
                json=frozen.exact_payload(restoring=False),
            ) as response:
                assert response.status == 201
            assert backend.cold_consign == "25.0"
            original["cold_consign"] = backend.cold_consign

            policy.arm_restoration(confirmed_by_push=True, owner_approved=True)
            async with gateway.request(
                "POST",
                ENDPOINT,
                params=params,
                json=frozen.exact_payload(restoring=True),
            ) as response:
                assert response.status == 201
            assert backend.cold_consign == "24.0"

            with pytest.raises(LabStopped):
                async with gateway.request(
                    "POST",
                    ENDPOINT,
                    params=params,
                    json=frozen.exact_payload(restoring=True),
                ):
                    pass
        assert backend.controls == 2
        assert gateway.observations["information_accepted"] == 1
        assert [x["kind"] for x in timeline.export()] == [
            "info_admitted",
            "info_result",
            "control_admitted",
            "control_result",
            "control_admitted",
            "control_result",
        ]
        records = [json.loads(line) for line in journal.path.read_text().splitlines()]
        assert [x["kind"] for x in records] == ["claim", "attempt", "attempt"]
    finally:
        journal.close()
        await protected.session.close()
        await backend.close()


@pytest.mark.asyncio
async def test_synthetic_fixture_still_denies_controls_unless_opted_in(
    tmp_path, socket_enabled
):
    backend = await SyntheticBackend(tmp_path).start()
    try:
        from aiohttp import ClientSession

        async with ClientSession(connector=backend.connector(trusted=True)) as client:
            async with client.post(
                ENDPOINT,
                json={
                    "event": {
                        "cgi": "modmaquina",
                        "device_id": "device",
                        "option": "P7",
                        "value": "25.0",
                    }
                },
            ) as response:
                assert response.status == 405
        assert backend.controls == 1
        assert backend.cold_consign == "24"
    finally:
        await backend.close()
