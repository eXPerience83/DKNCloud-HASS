"""Run a single approved Sleep experiment through the existing LAB gateway."""

import argparse
import asyncio
import json
import os
import re
import signal
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import ClientOSError, ClientTimeout, ServerDisconnectedError

from custom_components.airzoneclouddaikin import _is_online
from scripts.lab_policy import ReadOnlyPolicy
from scripts.protected_lab import LabStopped, LabSupervisor, create_gateway
from scripts.sleep_lab_grant import (
    authorize,
    private_json,
    source_digest,
    write_private,
)
from scripts.sleep_lab_plan import AUTOMATIC_SCENARIOS, SCENARIOS, SLEEP_PUT, make_plan

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://dkn.airzonecloud.com"


def power_payload(step, device):
    return {"event": step.payload["event"] | {"device_id": device}}


class SleepPolicy(ReadOnlyPolicy):
    """Exact REST scope, with the existing transport and no WebSockets."""

    def __init__(self, device, site, plan, deadline):
        super().__init__({device}, {site}, "", enabled=True)
        self.deadline = deadline
        self.device, self.site = device, site
        self.writes = [
            ("PUT", f"/devices/{device}", {"sleep_time": plan.sleep_minutes}),
            ("PUT", f"/devices/{device}", SLEEP_PUT),
        ]
        if plan.intervention.control:
            self.writes.append(
                ("POST", "/events", power_payload(plan.intervention, device))
            )
        self.written = 0
        self.login = 0
        self.discovery = 0

    def validate_socket(self, url):
        raise PermissionError("Sleep permits REST only")

    def validate(self, method, url, *, params=None, json=None, data=None):
        if data is not None or not url.startswith(BASE + "/"):
            raise PermissionError("Operation denied")
        path = url[len(BASE) :]
        if (method, path) == ("POST", "/users/sign_in") and not self.login:
            return super().validate(method, url, params=params, json=json)
        keys = {"user_email", "user_token"}
        if path != "/events":
            keys.add("format")
        if path == "/devices":
            keys.add("installation_id")
        if (
            type(params) is not dict
            or set(params) != keys
            or any(type(value) is not str or not value for value in params.values())
            or params.get("format", "json") != "json"
            or params.get("installation_id", self.site) != self.site
        ):
            raise PermissionError("Request scope denied")
        if method == "GET" and json is None:
            if path == "/devices" or (
                path == "/installation_relations" and not self.discovery
            ):
                return
        if (
            self.written < len(self.writes)
            and (method, path, json) == self.writes[self.written]
            and ("sleep_time" not in json or type(json["sleep_time"]) is int)
        ):
            return
        raise PermissionError("Operation denied")

    def reserve(self, method, url):
        super().reserve(method, url)
        if url == BASE + "/users/sign_in":
            self.login += 1
        elif url == BASE + "/installation_relations":
            self.discovery += 1
        elif method != "GET":
            self.written += 1


def scoped_row(value, wrapper, identity, expected):
    rows = value.get(wrapper) if isinstance(value, dict) else value
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
        or type(rows[0].get(identity)) not in {str, int}
        or str(rows[0].get(identity)) != expected
    ):
        raise LabStopped("Scope mismatch")
    return rows[0]


async def measure(
    plan,
    request,
    check,
    wait,
    log,
    email,
    password,
    device,
    site,
    now=time.monotonic,
    panel_off=None,
    physical_off=None,
    physical_on=None,
):
    """Execute the fixed timeline with injectable timing and transport for tests."""

    async def call(
        label, method, path, *, params=None, body=None, structured=True, due=None
    ):
        check()
        if due is not None and now() > due + 3:
            raise LabStopped("Scheduled command missed")
        return await request(
            label, method, path, params=params, body=body, structured=structured
        )

    login = await call(
        "login", "POST", "/users/sign_in", body={"email": email, "password": password}
    )
    token = login.get("authentication_token") or login.get("user", {}).get(
        "authentication_token"
    )
    if not isinstance(token, str) or not token:
        raise LabStopped("Login unconfirmed")
    auth = {"user_email": email, "user_token": token}
    formatted = auth | {"format": "json"}
    value = await call(
        "installation", "GET", "/installation_relations", params=formatted
    )
    scoped_row(value, "installation_relations", "installation_id", site)

    async def read():
        value = await call(
            "snapshot", "GET", "/devices", params=formatted | {"installation_id": site}
        )
        row = scoped_row(value, "devices", "id", device)
        state = {
            key: str(row.get(key))
            for key in ("power", "mode", "cold_speed", "scenary", "sleep_time")
        }
        online = (
            _is_online(row, datetime.now(UTC))
            if row.get("connection_date")
            else (str(row.get("online")).lower() in {"true", "1"})
        )
        if (
            state["power"] not in {"0", "1"}
            or state["mode"] != plan.required_mode
            or state["cold_speed"] != str(plan.required_fan_speed)
            or state["scenary"] not in {"occupied", "sleep"}
            or not re.fullmatch(r"[0-9]{1,4}", state["sleep_time"])
            or not online
        ):
            raise LabStopped("Unexpected device state")
        log(
            "rest",
            **state,
            online=online,
        )
        return state

    power = str(plan.required_start_power)

    def require(state, scene, *, duration=True):
        if (
            state["power"] != power
            or state["scenary"] != scene
            or (duration and state["sleep_time"] != str(plan.sleep_minutes))
        ):
            raise LabStopped("Precondition failed")

    path = f"/devices/{device}"
    require(await read(), plan.required_start_scene, duration=False)
    await call(
        "duration",
        "PUT",
        path,
        params=formatted,
        body={"sleep_time": plan.sleep_minutes},
        structured=False,
    )
    require(await read(), plan.required_start_scene)
    await call("scene", "PUT", path, params=formatted, body=SLEEP_PUT, structured=False)
    start = now()
    log("t0")
    require(await read(), "sleep")
    intervention = plan.intervention
    offset = intervention.at_second

    async def send_power(due):
        await call(
            "power",
            "POST",
            "/events",
            params=auth,
            body=power_payload(intervention, device),
            structured=False,
            due=due,
        )

    if plan.wait_for_off_seconds:
        if physical_off is None or physical_on is None:
            raise LabStopped("Physical observation unavailable")
        confirmed = await physical_off(start + plan.wait_for_off_seconds)
        if not start <= confirmed <= start + plan.wait_for_off_seconds:
            raise LabStopped("Physical OFF outside measurement window")
        state = await read()
        if state["power"] != "0" or state["sleep_time"] != str(plan.sleep_minutes):
            raise LabStopped("Physical OFF not corroborated by GET")
        await wait(confirmed + offset)
        wake_dispatch = now()
        await send_power(confirmed + offset)
        wake = now()
        log("wake_ack")
        observed_on = await physical_on(wake_dispatch, wake + 20)
        if not wake_dispatch <= observed_on <= wake + 20:
            raise LabStopped("Second physical ON outside confirmation window")
        for second in range(10, plan.observation_seconds + 1, 10):
            await wait(observed_on + second)
            if (await read())["sleep_time"] != str(plan.sleep_minutes):
                raise LabStopped("Unexpected Sleep duration")
        log("observation_closed", classification="pending_physical_correlation")
        return

    # Keep polling across both candidate expiry windows. Leave spacing before P1.
    for second in range(30, plan.observation_seconds + 1, 30):
        await wait(start + second)
        if second == offset:
            if not intervention.control:
                if panel_off is None:
                    raise LabStopped("Panel confirmation unavailable")
                await panel_off()
                await read()
                continue
            await send_power(start + second)
            continue
        state = await read()
        if second < offset:
            require(state, "sleep")
        elif state["sleep_time"] != str(plan.sleep_minutes):
            raise LabStopped("Unexpected observation")
    if plan.observation_seconds % 30:
        raise LabStopped("Invalid observation schedule")
    log("observation_closed", classification="pending_physical_correlation")


def local_input(*, require_scope=True):
    path = ROOT / "secrets/dkn.env"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("Private credential file required")
        keys = (
            "AIRZONE_USERNAME",
            "AIRZONE_PASSWORD",
            "AIRZONE_DEVICE_ID",
            "AIRZONE_INSTALLATION_ID",
        )
        values = {}
        for line in stream:
            key, _, value = line.strip().partition("=")
            if key in keys and (require_scope or key in keys[:2]):
                if key in values or (not value and key in keys[:2]):
                    raise PermissionError("Invalid local configuration")
                values[key] = value
        email, password = (values[key] for key in keys[:2])
    if not require_scope:
        return email, password
    scope_file = ROOT / "secrets/sleep-scope.json"
    if scope_file.exists():
        scope = private_json(scope_file)
        device, site = scope["device_id"], scope["installation_id"]
    else:
        device, site = (values[key] for key in keys[2:])
    if not all(
        type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]+", value)
        for value in (device, site)
    ):
        raise PermissionError("Invalid local scope")
    return email, password, device, site


class AmbiguousDelivery(RuntimeError):
    """A request may have reached the backend."""


async def fail_on_disconnect(request, handler):
    """Stop aiohttp's retry loop at the public middleware boundary."""
    try:
        return await handler(request)
    except ClientOSError, ServerDisconnectedError:
        pass
    raise AmbiguousDelivery("HTTP delivery unconfirmed")


async def run(plan, journal, email, password, device, site):
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    heartbeat = started
    panel_confirmation = asyncio.Event()
    panel_requested = False
    natural_off = asyncio.Event()
    wake_on = asyncio.Event()
    physical_times = {}

    def log(event, **fields):
        journal.write(
            json.dumps(
                {
                    "seconds": round(time.monotonic() - started, 3),
                    "utc": datetime.now(UTC).isoformat(),
                    "event": event,
                    **fields,
                }
            )
            + "\n"
        )
        journal.flush()
        os.fsync(journal.fileno())

    operation = None
    power_headers_at = None

    class SleepSupervisor(LabSupervisor):
        def response(self, status):
            log(
                "http", operation=operation, status=status, accepted=200 <= status < 300
            )
            super().response(status)

    supervisor = SleepSupervisor(auth_limit=1, state_limit=plan.state_budget)
    # Only this owned session uses the trial ceiling.
    supervisor.deadline = started + plan.hard_deadline_seconds
    policy = SleepPolicy(device, site, plan, supervisor.deadline)

    async def headers_sent(_session, _context, _headers):
        nonlocal power_headers_at
        if operation == "power":
            power_headers_at = time.monotonic()
        log("http_sent", operation=operation)

    gateway = await create_gateway(
        policy,
        supervisor,
        middlewares=(fail_on_disconnect,),
        on_headers_sent=headers_sent,
        empty_put_ack=True,
    )
    task = asyncio.current_task()

    def stop():
        if not task.cancelling():
            loop.remove_reader(sys.stdin.fileno())
            task.cancel()

    def operator():
        nonlocal heartbeat
        try:
            command = os.read(sys.stdin.fileno(), 4096).strip()
        except OSError:
            stop()
            return
        if (
            command == b"panel-off"
            and panel_requested
            and not panel_confirmation.is_set()
        ):
            try:
                log("panel", action="power_off", physical_power="off")
            except OSError:
                stop()
                return
            heartbeat = time.monotonic()
            panel_confirmation.set()
            return
        if command == b"interference":
            try:
                log("interference")
            finally:
                stop()
            return
        if command not in {b"ok", b"on", b"off"}:
            stop()
            return
        heartbeat = time.monotonic()
        if command != b"ok":
            try:
                power = command.decode("ascii")
                physical_times[power] = heartbeat
                log("physical", power=power)
                (natural_off if power == "off" else wake_on).set()
            except OSError:
                stop()

    def check():
        supervisor.check()
        if time.monotonic() - heartbeat >= 45:
            raise LabStopped("Observer timeout")

    async def watch():
        while True:
            await asyncio.sleep(0.25)
            try:
                check()
            except LabStopped:
                try:
                    log("supervision_stop", reason=supervisor.reason or "heartbeat")
                finally:
                    stop()
                return

    async def request(label, method, path, *, params, body, structured):
        nonlocal operation
        operation = label
        log("attempt", operation=label)
        async with gateway.request(
            method,
            BASE + path,
            params=params,
            json=body,
            headers={"x-requested-with": "XMLHttpRequest"},
            timeout=ClientTimeout(total=6),
        ) as response:
            if not 200 <= response.status < 300:
                raise LabStopped("HTTP failure")
            return await response.json() if structured else None

    async def panel_off():
        nonlocal panel_requested
        log("panel_requested", action="power_off")
        print("Turn OFF at the physical panel now, then enter panel-off.", flush=True)
        panel_requested = True
        try:
            async with asyncio.timeout(15):
                await panel_confirmation.wait()
        finally:
            panel_requested = False

    async def physical_off(deadline):
        print(
            "When natural OFF is physically visible, enter off. After wake enter on.",
            flush=True,
        )
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            await natural_off.wait()
        return physical_times["off"]

    async def physical_on(earliest, deadline):
        if physical_times.get("on", -1) < earliest:
            wake_on.clear()
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            await wake_on.wait()
        return physical_times["on"]

    async def wait(until):
        await asyncio.sleep(max(0, until - time.monotonic()))

    async with gateway.protected.session:
        loop.add_reader(sys.stdin.fileno(), operator)
        loop.add_signal_handler(signal.SIGTERM, stop)
        watcher = asyncio.create_task(watch())
        try:
            with gateway.enforce():
                async with asyncio.timeout_at(loop.time() + plan.hard_deadline_seconds):
                    await measure(
                        plan,
                        request,
                        check,
                        wait,
                        log,
                        email,
                        password,
                        device,
                        site,
                        panel_off=panel_off,
                        physical_off=physical_off,
                        physical_on=physical_on,
                    )
                    if plan.intervention.control:
                        expected = (
                            "on"
                            if plan.intervention.payload["event"]["value"] == "1"
                            else "off"
                        )
                        observed = physical_times.get(expected)
                        if (
                            power_headers_at is None
                            or observed is None
                            or observed < power_headers_at
                        ):
                            raise LabStopped("Physical power change unconfirmed")
        except (Exception, asyncio.CancelledError) as error:
            log("stopped", kind=type(error).__name__, classification="inconclusive")
            raise LabStopped("Session stopped") from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            loop.remove_reader(sys.stdin.fileno())
            loop.remove_signal_handler(signal.SIGTERM)
            log("restoration_required")


def execute_trial(scenario, grant, directory, credentials):
    plan = make_plan(scenario)
    email, password, device, site = credentials
    if (
        input(
            "Confirm physical baseline and exclusive control. Type the scenario name: "
        )
        != scenario
    ):
        raise LabStopped("Operator declined")
    folder, lock = authorize(
        grant, directory, ROOT, scenario, plan.hard_deadline_seconds, (device, site)
    )
    try:
        fd = os.open(
            folder / "evidence.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(fd, "w") as journal:
            print(
                "Enter ok, on or off every 30 seconds. Enter interference or stop to cancel.",
                flush=True,
            )
            asyncio.run(run(plan, journal, email, password, device, site))
    finally:
        print(
            "Confirm physical OFF and restore occupied separately. Check for delayed commands."
        )
        print(f"Private evidence directory: {folder.name}")
    if (
        input(
            "Only after corroborating the outcome and independently verifying physical OFF and raw occupied, type restored: "
        )
        != "restored"
    ):
        raise LabStopped("Restoration unconfirmed")
    write_private(
        folder / "restored.json", {"physical_off": True, "raw_occupied": True}
    )
    lock.rmdir()


def execute_sequence(grants, execute, receipt, approval):
    """Publish completion only after every trial and its independent restoration."""
    receipt.unlink(missing_ok=True)
    for scenario, grant in zip(AUTOMATIC_SCENARIOS, grants, strict=True):
        execute(scenario, grant)
    write_private(receipt, approval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scenario", choices=SCENARIOS)
    mode.add_argument("--automatic", nargs=3, metavar=("A", "C", "F"), type=Path)
    parser.add_argument("--grant", type=Path)
    args = parser.parse_args(argv)
    if args.digest:
        print(source_digest(ROOT))
        return 0
    if not (
        sys.stdin.isatty()
        and ((args.scenario and args.grant) or (args.automatic and not args.grant))
    ):
        parser.error(
            "Live execution requires a private grant per scenario and an operator TTY"
        )
    try:
        if args.automatic and len({grant.resolve() for grant in args.automatic}) != 3:
            raise LabStopped("Separate grants required")
        credentials = local_input()
        directory = ROOT / "secrets/sleep-sessions"
        # Read the directory requirements before taking the campaign lock.
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise LabStopped("Private session directory required")
        lock = directory / "campaign"
        lock.mkdir(mode=0o700)
        try:
            receipt = directory / "automatic-restored.json"
            approval = {
                "source_digest": source_digest(ROOT),
                "device_id": credentials[2],
                "installation_id": credentials[3],
            }
            if args.automatic:
                execute_sequence(
                    args.automatic,
                    lambda scenario, grant: execute_trial(
                        scenario, grant, directory, credentials
                    ),
                    receipt,
                    approval,
                )
            else:
                if args.scenario == "on_sleep_then_panel_off":
                    if (
                        private_json(receipt) != approval
                        or input(
                            "Review A, C and F before B. Type reviewed to confirm: "
                        )
                        != "reviewed"
                    ):
                        raise LabStopped("Review of A, C and F required")
                else:
                    receipt.unlink(missing_ok=True)
                execute_trial(args.scenario, args.grant, directory, credentials)
        finally:
            lock.rmdir()
    except Exception, KeyboardInterrupt:
        print(
            "Sleep LAB stopped or refused. Review the private journal and any pending restoration.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
