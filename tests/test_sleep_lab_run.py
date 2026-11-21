"""Offline verification of the supervised Sleep timeline and boundary."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from scripts.sleep_lab_grant import authorize, write_private
from scripts.sleep_lab_plan import SLEEP_PUT, SLEEP_TIME_PUT, make_plan
from scripts.sleep_lab_run import BASE, SCENARIOS, SleepPolicy, measure


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("failure", [None, "http", "network", "ambiguous", "observer"])
async def test_measure(scenario, failure):
    plan = make_plan(scenario)
    offset = 240 if plan.required_start_power == 0 else 120
    second = 0
    state = {
        "id": "unit",
        "online": True,
        "mode": "3",
        "cold_speed": 1,
        "scenary": "occupied",
        "power": plan.required_start_power,
        "sleep_time": 30,
    }
    calls, events = [], []
    policy = SleepPolicy(
        "unit", "site", plan.required_start_power, time.monotonic() + 60
    )

    async def wait(until):
        nonlocal second
        second = until

    def check():
        if failure == "observer" and second >= 60:
            raise PermissionError

    def log(event, **fields):
        events.append((second, event, fields))

    async def request(label, method, path, *, params, body, structured):
        url = BASE + path
        policy.validate(method, url, params=params, json=body)
        policy.reserve(method, url)
        calls.append((second, label))
        if label == "login":
            return {"user": {"authentication_token": "fixture"}}
        if label == "installation":
            return [{"installation_id": "site"}]
        if label == "snapshot":
            return [] if failure == "ambiguous" and second == 30 else [state.copy()]
        if label == "duration":
            state.update(SLEEP_TIME_PUT)
        if label == "scene":
            if failure == "http":
                raise PermissionError
            state.update(SLEEP_PUT["device"])
        if label == "power":
            state["power"] = body["event"]["value"]
            if failure == "network":
                raise OSError
            if offset == 120:
                state["scenary"] = "occupied"

    args = (plan, request, check, wait, log, "fixture", "fixture", "unit", "site")
    if failure:
        with pytest.raises((PermissionError, OSError)):
            await measure(*args, now=lambda: second)
        assert sum(label == "power" for _, label in calls) == (failure == "network")
        assert all(event != "observation_closed" for _, event, _ in events)
    else:
        await measure(*args, now=lambda: second)
        assert [item for item in calls if item[1] == "power"] == [(offset, "power")]
        assert events[-1][:2] == (plan.observation_seconds, "observation_closed")
        assert policy.written == 3
        assert policy.counts["state"] == (26 if offset == 240 else 19)


def test_policy_denies_out_of_scope_operations():
    policy = SleepPolicy("unit", "site", 0, time.monotonic() + 60)
    auth = {"user_email": "fixture", "user_token": "fixture", "format": "json"}
    for method, url, params, body in [
        ("GET", BASE + "/devices", auth, None),
        ("GET", BASE + "/devices", auth | {"installation_id": "other"}, None),
        ("GET", BASE + "/devices?test=1", auth | {"installation_id": "site"}, None),
        ("PUT", BASE + "/devices/other", auth, SLEEP_TIME_PUT),
        ("PUT", BASE + "/devices/unit", auth, SLEEP_PUT),
        ("PUT", BASE + "/devices/unit", auth, {"sleep_time": 6}),
        ("DELETE", BASE + "/devices/unit", auth, None),
        ("POST", BASE + "/events", auth, {"event": {"option": "P2"}}),
    ]:
        with pytest.raises(PermissionError):
            policy.validate(method, url, params=params, json=body)
    with pytest.raises(PermissionError):
        policy.validate_socket("wss://dkn.airzonecloud.com:8080")


def test_grant_is_scoped_and_single_use(tmp_path, monkeypatch):
    import scripts.sleep_lab_grant as grants

    monkeypatch.setattr(grants, "source_digest", lambda _: "reviewed-source")
    directory = tmp_path / "sessions"
    directory.mkdir(mode=0o700)
    grant = tmp_path / "grant.json"
    write_private(
        grant,
        {
            "approved": True,
            "scenario": SCENARIOS[0],
            "device_id": "unit",
            "installation_id": "site",
            "nonce": "a" * 64,
            "source_digest": "reviewed-source",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        },
    )
    with pytest.raises(PermissionError):
        authorize(grant, directory, tmp_path, SCENARIOS[1], 570, ("unit", "site"))
    _, lock = authorize(grant, directory, tmp_path, SCENARIOS[0], 780, ("unit", "site"))
    lock.rmdir()
    with pytest.raises(FileExistsError):
        authorize(grant, directory, tmp_path, SCENARIOS[0], 780, ("unit", "site"))


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_schedule_through_loopback_tls(scenario, socket_enabled, tmp_path):
    from aiohttp import web

    from scripts.lab_tls_backend import SyntheticBackend
    from scripts.protected_lab import LabSupervisor, create_gateway

    plan = make_plan(scenario)
    state = {
        "id": "unit",
        "online": True,
        "mode": "3",
        "cold_speed": 1,
        "scenary": "occupied",
        "power": plan.required_start_power,
        "sleep_time": 30,
    }
    writes = []

    class SleepBackend(SyntheticBackend):
        async def handle(self, request):
            if request.path == "/devices":
                return web.json_response([state])
            if request.method == "PUT":
                body = await request.json()
                writes.append(body)
                state.update(body.get("device", body))
                return web.json_response({})
            if request.path == "/events":
                event = (await request.json())["event"]
                writes.append(event["option"])
                state["power"] = event["value"]
                return web.Response(status=204)
            return await super().handle(request)

    second = 0

    async def wait(until):
        nonlocal second
        second = until

    backend = await SleepBackend(tmp_path).start()
    try:
        supervisor = LabSupervisor(spacing=0, auth_limit=1, state_limit=30)
        policy = SleepPolicy(
            "unit", "site", plan.required_start_power, supervisor.deadline
        )
        gateway = await create_gateway(
            policy, supervisor, connector=backend.connector()
        )

        async def request(label, method, path, *, params, body, structured):
            async with gateway.request(
                method, BASE + path, params=params, json=body
            ) as response:
                assert 200 <= response.status < 300
                return await response.json() if structured else None

        async with gateway.protected.session:
            with gateway.enforce():
                await measure(
                    plan,
                    request,
                    supervisor.check,
                    wait,
                    lambda *a, **kw: None,
                    "fixture",
                    "fixture",
                    "unit",
                    "site",
                    now=lambda: second,
                )
        assert gateway.protected.session.closed
        assert writes == [SLEEP_TIME_PUT, SLEEP_PUT, "P1"]
        assert second == plan.observation_seconds
    finally:
        await backend.close()
