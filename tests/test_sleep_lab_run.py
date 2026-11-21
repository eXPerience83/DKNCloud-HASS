"""Offline verification of the supervised Sleep timeline and boundary."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from scripts.sleep_lab_grant import authorize, write_private
from scripts.sleep_lab_plan import REWAKE_SCENARIO, SLEEP_PUT, SLEEP_TIME_PUT, make_plan
from scripts.sleep_lab_run import BASE, SCENARIOS, SleepPolicy, measure


@pytest.mark.parametrize(
    "scenario", [name for name in SCENARIOS if name != REWAKE_SCENARIO]
)
@pytest.mark.parametrize(
    "failure", [None, "http", "network", "ambiguous", "observer", "mode_alias"]
)
async def test_measure(scenario, failure):
    plan = make_plan(scenario)
    offset = plan.intervention.at_second
    remote = plan.intervention.control
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
    policy = SleepPolicy("unit", "site", plan, time.monotonic() + 60)

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
            if failure == "mode_alias" and second == 30:
                state["mode"] = "8"
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

    async def panel_off():
        state["power"] = "0"
        log("panel", action="power_off")
        if failure == "network":
            raise OSError

    args = (plan, request, check, wait, log, "fixture", "fixture", "unit", "site")
    if failure:
        with pytest.raises((PermissionError, OSError)):
            await measure(*args, now=lambda: second, panel_off=panel_off)
        assert sum(label == "power" for _, label in calls) == (
            failure == "network" and remote
        )
        assert all(event != "observation_closed" for _, event, _ in events)
    else:
        await measure(*args, now=lambda: second, panel_off=panel_off)
        assert [item for item in calls if item[1] == "power"] == (
            [(offset, "power")] if remote else []
        )
        assert [second for second, event, _ in events if event == "panel"] == (
            [] if remote else [offset]
        )
        assert events[-1][:2] == (plan.observation_seconds, "observation_closed")
        assert policy.written == 2 + remote
        assert policy.counts["state"] == (26 if offset == 240 else 19)


def test_policy_denies_out_of_scope_operations():
    policy = SleepPolicy("unit", "site", make_plan(SCENARIOS[0]), time.monotonic() + 60)
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
    expired = grants.private_json(grant) | {
        "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "nonce": "b" * 64,
    }
    write_private(grant, expired)
    with pytest.raises(PermissionError, match="Invalid Sleep authorization"):
        authorize(grant, directory, tmp_path, SCENARIOS[0], 780, ("unit", "site"))


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [(scenario, "rearm") for scenario in SCENARIOS]
    + [
        (REWAKE_SCENARIO, outcome)
        for outcome in (
            "no_off_confirmation",
            "no_second_on",
            "no_rearm",
            "occupied",
            "duration_rejected",
            "empty_get",
        )
    ],
)
async def test_schedule_through_loopback_tls(
    scenario, outcome, socket_enabled, tmp_path
):
    from contextlib import nullcontext

    from aiohttp import web

    from scripts.lab_tls_backend import SyntheticBackend
    from scripts.protected_lab import LabStopped, LabSupervisor, create_gateway

    plan = make_plan(scenario)
    rewake = scenario == REWAKE_SCENARIO
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
    writes, evidence = [], []

    class SleepBackend(SyntheticBackend):
        async def handle(self, request):
            if request.path == "/devices":
                if outcome == "empty_get":
                    return web.Response(status=200)
                if rewake and second == 60:
                    state["power"] = "0"
                    if outcome == "occupied":
                        state["scenary"] = "occupied"
                if rewake and outcome == "rearm" and second >= 140:
                    state["power"] = "0"
                return web.json_response([state])
            if request.method == "PUT":
                body = await request.json()
                writes.append(body)
                if outcome != "duration_rejected":
                    state.update(body.get("device", body))
                return web.Response(status=200)
            if request.path == "/events":
                event = (await request.json())["event"]
                writes.append((event["option"], second))
                state["power"] = event["value"]
                return web.Response(status=204)
            return await super().handle(request)

    async def wait(until):
        nonlocal second
        second = until

    async def panel_off():
        state["power"] = "0"

    async def physical_off(deadline):
        if outcome == "no_off_confirmation":
            await wait(deadline)
            raise TimeoutError
        await wait(60)
        return second

    async def physical_on(earliest, deadline):
        if outcome == "no_second_on":
            await wait(deadline)
            raise TimeoutError
        await wait(81)
        return second

    def log(event, **fields):
        evidence.append((event, fields))

    failure = {
        "no_off_confirmation": pytest.raises(TimeoutError),
        "no_second_on": pytest.raises(TimeoutError),
        "duration_rejected": pytest.raises(LabStopped, match="Precondition failed"),
        "empty_get": pytest.raises(LabStopped),
    }.get(outcome, nullcontext())

    backend = await SleepBackend(tmp_path).start()
    try:
        supervisor = LabSupervisor(
            spacing=0, auth_limit=1, state_limit=plan.state_budget
        )
        policy = SleepPolicy("unit", "site", plan, supervisor.deadline)
        gateway = await create_gateway(
            policy, supervisor, connector=backend.connector(), empty_put_ack=True
        )

        async def request(label, method, path, *, params, body, structured):
            nonlocal second
            async with gateway.request(
                method, BASE + path, params=params, json=body
            ) as response:
                assert 200 <= response.status < 300
                value = await response.json() if structured else None
                if rewake and label == "snapshot" and second == 60:
                    second += 4  # GET receipt is later than physical confirmation.
                return value

        async with gateway.protected.session:
            with gateway.enforce(), failure:
                await measure(
                    plan,
                    request,
                    supervisor.check,
                    wait,
                    log,
                    "fixture",
                    "fixture",
                    "unit",
                    "site",
                    now=lambda: second,
                    panel_off=panel_off,
                    physical_off=physical_off,
                    physical_on=physical_on,
                )
        assert gateway.protected.session.closed
        base_writes = [{"sleep_time": plan.sleep_minutes}, SLEEP_PUT]
        if outcome == "empty_get":
            assert writes == []
        elif outcome == "duration_rejected":
            assert writes == base_writes[:1]
        elif outcome == "no_off_confirmation":
            assert writes == base_writes
        else:
            wake = 80 if rewake else plan.intervention.at_second
            assert writes == base_writes + (
                [("P1", wake)] if plan.intervention.control else []
            )
            if outcome == "no_second_on":
                assert second == wake + 20
                return
            assert second == plan.observation_seconds + (81 if rewake else 0)
            if rewake:
                final = [fields for event, fields in evidence if event == "rest"][-1]
                assert final["power"] == ("0" if outcome == "rearm" else "1")
                assert final["scenary"] == (
                    "occupied" if outcome == "occupied" else "sleep"
                )
        assert policy.counts["state"] <= plan.state_budget
    finally:
        await backend.close()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_exact_event_boundary(scenario):
    from scripts.sleep_lab_run import power_payload

    plan = make_plan(scenario)
    policy = SleepPolicy("unit", "site", plan, time.monotonic() + 60)
    url = BASE + "/events"
    auth = {"user_email": "fixture", "user_token": "fixture"}
    policy.reserve("PUT", BASE + "/devices/unit")
    policy.reserve("PUT", BASE + "/devices/unit")
    canonical = power_payload(make_plan("on_sleep_then_off").intervention, "unit")
    if plan.intervention.control:
        canonical = power_payload(plan.intervention, "unit")
        policy.validate("POST", url, params=auth, json=canonical)
    else:
        with pytest.raises(PermissionError):
            policy.validate("POST", url, params=auth, json=canonical)
    event = canonical["event"]
    for change in (
        {"option": "P2"},
        {"device_id": "other"},
        {"cgi": "infomaquina"},
        {"value": "0" if event["value"] == "1" else "1"},
        {"extra": "forbidden"},
    ):
        with pytest.raises(PermissionError):
            policy.validate("POST", url, params=auth, json={"event": event | change})
    if plan.intervention.control:
        policy.reserve("POST", url)
    with pytest.raises(PermissionError):
        policy.validate("POST", url, params=auth, json=canonical)


@pytest.mark.parametrize("rows", [[{"id": "other"}], [{"id": "unit"}, {"id": "other"}]])
def test_measurement_rejects_ambiguous_or_wrong_scope(rows):
    from scripts.sleep_lab_run import scoped_row

    with pytest.raises(PermissionError):
        scoped_row(rows, "devices", "id", "unit")
    with pytest.raises(PermissionError):
        scoped_row(
            [{"installation_id": "site"}, {"installation_id": "other"}],
            "installation_relations",
            "installation_id",
            "site",
        )


@pytest.mark.parametrize(
    "trigger", ["eof", "sigterm", "heartbeat", "deadline", "panel", "interference"]
)
async def test_run_supervision_and_session_cleanup(trigger, monkeypatch, tmp_path):
    import asyncio
    import json
    import os
    import signal
    from contextlib import nullcontext
    from types import SimpleNamespace

    from scripts import protected_lab, sleep_lab_run as runner

    clock = SimpleNamespace(monotonic=lambda: 0)
    monkeypatch.setattr(runner, "time", clock)
    monkeypatch.setattr(protected_lab, "time", clock)
    loop = asyncio.get_running_loop()
    sigterm = {}
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, cb: sigterm.update({sig: cb})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", sigterm.pop)
    entered = asyncio.Event()
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd)
    monkeypatch.setattr(runner, "sys", SimpleNamespace(stdin=stream))

    class Session:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    session = Session()

    async def gateway(*args, **kwargs):
        return SimpleNamespace(
            enforce=nullcontext,
            protected=SimpleNamespace(session=session),
        )

    async def measure(*args, panel_off, **kwargs):
        entered.set()
        if trigger == "panel":
            await panel_off()
        else:
            await asyncio.Future()

    monkeypatch.setattr(runner, "create_gateway", gateway)
    monkeypatch.setattr(runner, "measure", measure)
    path = tmp_path / "evidence.jsonl"
    with stream, path.open("w") as journal:
        task = asyncio.create_task(
            runner.run(
                make_plan("on_sleep_then_panel_off"),
                journal,
                "private-email",
                "private-password",
                "unit",
                "site",
            )
        )
        try:
            async with asyncio.timeout(2):
                await entered.wait()
                if trigger == "eof":
                    os.close(write_fd)
                    write_fd = None
                elif trigger == "sigterm":
                    sigterm[signal.SIGTERM]()
                elif trigger in {"heartbeat", "deadline"}:
                    clock.monotonic = lambda: 46 if trigger == "heartbeat" else 571
                elif trigger == "interference":
                    os.write(write_fd, b"interference\n")
                else:
                    os.write(write_fd, b"panel-off\n")
                if trigger == "panel":
                    await task
                else:
                    with pytest.raises(runner.LabStopped):
                        await task
        finally:
            if write_fd is not None:
                os.close(write_fd)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert session.closed
    assert not sigterm
    assert records[-1]["event"] == "restoration_required"
    assert all("private-" not in str(record) for record in records)
    if trigger in {"heartbeat", "deadline"}:
        assert [r["reason"] for r in records if r["event"] == "supervision_stop"] == [
            "duration" if trigger == "deadline" else "heartbeat"
        ]
    if trigger == "panel":
        assert [r["event"] for r in records] == [
            "panel_requested",
            "panel",
            "restoration_required",
        ]
    else:
        assert records[-2]["classification"] == "inconclusive"


@pytest.mark.parametrize("failure", [401, 429, 500, "redirect", "disconnect"])
async def test_http_stop_and_no_replay(failure, socket_enabled, tmp_path):
    from aiohttp import web

    from scripts.lab_tls_backend import SyntheticBackend
    from scripts.protected_lab import LabStopped, LabSupervisor, create_gateway
    from scripts.sleep_lab_run import AmbiguousDelivery, fail_on_disconnect

    attempts = []

    class FailingBackend(SyntheticBackend):
        async def handle(self, request):
            attempts.append(request.method)
            if failure == "disconnect":
                request.transport.abort()
                return web.Response()
            if failure == "redirect":
                raise web.HTTPFound(BASE + "/events")
            return web.Response(status=failure)

    backend = await FailingBackend(tmp_path).start()
    try:
        supervisor = LabSupervisor(spacing=0)
        policy = SleepPolicy(
            "unit", "site", make_plan(SCENARIOS[0]), supervisor.deadline
        )
        gateway = await create_gateway(
            policy,
            supervisor,
            connector=backend.connector(),
            middlewares=(fail_on_disconnect,),
        )
        params = {"user_email": "fixture", "user_token": "fixture", "format": "json"}
        async with gateway.protected.session:
            with gateway.enforce(), pytest.raises((LabStopped, AmbiguousDelivery)):
                async with gateway.request(
                    "PUT", BASE + "/devices/unit", params=params, json=SLEEP_TIME_PUT
                ) as response:
                    if response.status >= 400:
                        raise LabStopped("HTTP failure")
        assert attempts == ["PUT"]
        assert policy.written == 1
        assert gateway.protected.session.closed
    finally:
        await backend.close()


@pytest.mark.parametrize("fail_at", [None, "on_sleep_then_off"])
def test_campaign_runs_in_order_and_requires_restoration(tmp_path, fail_at):
    from scripts.sleep_lab_run import execute_sequence

    receipt = tmp_path / "automatic-restored.json"
    write_private(receipt, {"old": True})
    calls = []

    def execute(scenario, grant):
        assert not receipt.exists()
        calls.append((scenario, grant))
        if scenario == fail_at:
            raise PermissionError("Restoration unconfirmed")

    expected = [
        ("off_sleep_then_on", "A"),
        ("on_sleep_then_off", "C"),
        (REWAKE_SCENARIO, "F"),
    ]
    if fail_at:
        with pytest.raises(PermissionError):
            execute_sequence(("A", "C", "F"), execute, receipt, {"approved": True})
        assert calls == expected[:2]
        assert not receipt.exists()
    else:
        execute_sequence(("A", "C", "F"), execute, receipt, {"approved": True})
        assert calls == expected
        assert receipt.is_file()


@pytest.mark.parametrize("receipt", ["missing", "wrong", "declined", "valid"])
def test_panel_trial_needs_prior_campaign_and_review(tmp_path, monkeypatch, receipt):
    from types import SimpleNamespace

    from scripts import sleep_lab_run as runner

    sessions = tmp_path / "secrets" / "sleep-sessions"
    sessions.mkdir(mode=0o700, parents=True)
    scope = {
        "source_digest": "reviewed",
        "device_id": "unit",
        "installation_id": "site",
    }
    if receipt != "missing":
        write_private(
            sessions / "automatic-restored.json",
            scope if receipt in {"declined", "valid"} else {},
        )
    calls = []
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "source_digest", lambda _: "reviewed")
    monkeypatch.setattr(
        runner, "local_input", lambda: ("fixture", "fixture", "unit", "site")
    )
    monkeypatch.setattr(
        runner,
        "sys",
        SimpleNamespace(
            argv=["runner", "--scenario", "on_sleep_then_panel_off", "--grant", "B"],
            stdin=SimpleNamespace(isatty=lambda: True),
            stderr=None,
        ),
    )
    monkeypatch.setattr(
        "builtins.input", lambda _: "decline" if receipt == "declined" else "reviewed"
    )
    monkeypatch.setattr(runner, "execute_trial", lambda *args: calls.append(args[0]))
    assert runner.main(["--scenario", "on_sleep_then_panel_off", "--grant", "B"]) == (
        0 if receipt == "valid" else 1
    )
    assert calls == (["on_sleep_then_panel_off"] if receipt == "valid" else [])


def test_unconfirmed_restore_keeps_active_lock(tmp_path, monkeypatch):
    from scripts import sleep_lab_run as runner

    folder = tmp_path / "trial"
    folder.mkdir()
    lock = tmp_path / "active"
    lock.mkdir()
    answers = iter(("off_sleep_then_on", "uncertain"))
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(runner, "authorize", lambda *args: (folder, lock))

    async def run(*args):
        pass

    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(PermissionError, match="Restoration unconfirmed"):
        runner.execute_trial(
            "off_sleep_then_on",
            tmp_path / "grant",
            tmp_path,
            ("fixture", "fixture", "unit", "site"),
        )
    assert lock.is_dir()
    assert not (folder / "restored.json").exists()
