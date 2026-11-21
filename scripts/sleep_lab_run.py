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
from scripts.sleep_lab_grant import authorize, private_json, source_digest
from scripts.sleep_lab_plan import SCENARIOS, SLEEP_PUT, SLEEP_TIME_PUT, make_plan

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
            ("PUT", f"/devices/{device}", SLEEP_TIME_PUT),
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
            or (duration and state["sleep_time"] != "5")
        ):
            raise LabStopped("Precondition failed")

    path = f"/devices/{device}"
    require(await read(), plan.required_start_scene, duration=False)
    await call(
        "duration", "PUT", path, params=formatted, body=SLEEP_TIME_PUT, structured=False
    )
    require(await read(), plan.required_start_scene)
    await call("scene", "PUT", path, params=formatted, body=SLEEP_PUT, structured=False)
    start = now()
    log("t0")
    require(await read(), "sleep")
    intervention = plan.intervention
    offset = intervention.at_second
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
            await call(
                "power",
                "POST",
                "/events",
                params=auth,
                body=power_payload(intervention, device),
                structured=False,
                due=start + second,
            )
            continue
        state = await read()
        if second < offset:
            require(state, "sleep")
        elif state["sleep_time"] != "5":
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

    class SleepSupervisor(LabSupervisor):
        def response(self, status):
            log(
                "http", operation=operation, status=status, accepted=200 <= status < 300
            )
            super().response(status)

    supervisor = SleepSupervisor(
        auth_limit=1, state_limit=6 + plan.observation_seconds // 30
    )
    # Only this owned session uses the trial ceiling.
    supervisor.deadline = started + plan.hard_deadline_seconds
    policy = SleepPolicy(device, site, plan, supervisor.deadline)

    async def headers_sent(_session, _context, _headers):
        log("http_sent", operation=operation)

    gateway = await create_gateway(
        policy,
        supervisor,
        middlewares=(fail_on_disconnect,),
        on_headers_sent=headers_sent,
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
                log("physical", power=command.decode("ascii"))
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
                    )
        except (Exception, asyncio.CancelledError) as error:
            log("stopped", kind=type(error).__name__, classification="inconclusive")
            raise LabStopped("Session stopped") from None
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            loop.remove_reader(sys.stdin.fileno())
            loop.remove_signal_handler(signal.SIGTERM)
            log("restoration_required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--grant", type=Path)
    args = parser.parse_args()
    if args.digest:
        print(source_digest(ROOT))
        return 0
    if not (args.scenario and args.grant and sys.stdin.isatty()):
        parser.error(
            "Live execution requires a scenario, private grant and operator TTY"
        )
    plan = make_plan(args.scenario)
    try:
        email, password, device, site = local_input()
        if (
            input(
                "Confirm physical baseline and exclusive control. Type the scenario name: "
            )
            != plan.name
        ):
            raise LabStopped("Operator declined")
        folder, lock = authorize(
            args.grant,
            ROOT / "secrets/sleep-sessions",
            ROOT,
            plan.name,
            plan.hard_deadline_seconds,
            (device, site),
        )
        try:
            fd = os.open(
                folder / "evidence.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(fd, "w") as journal:
                print(
                    "Enter ok, on or off every 30 seconds. Enter stop to cancel.",
                    flush=True,
                )
                asyncio.run(run(plan, journal, email, password, device, site))
        finally:
            lock.rmdir()
            print(
                "Confirm physical OFF and restore occupied separately. Check for delayed commands."
            )
    except Exception, KeyboardInterrupt:
        print(
            "Sleep LAB stopped or refused. Review the private journal if created.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
