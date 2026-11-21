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
from pathlib import Path

from aiohttp import ClientTimeout

from scripts.lab_policy import ReadOnlyPolicy
from scripts.protected_lab import LabStopped, LabSupervisor, create_gateway
from scripts.sleep_lab_grant import authorize, source_digest
from scripts.sleep_lab_plan import SLEEP_PUT, SLEEP_TIME_PUT, make_plan

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://dkn.airzonecloud.com"
SCENARIOS = ("off_sleep_then_on", "on_sleep_then_off")


class SleepPolicy(ReadOnlyPolicy):
    """Exact REST scope, with the existing transport and no WebSockets."""

    def __init__(self, device, site, power, deadline):
        super().__init__({device}, {site}, "", enabled=True)
        self.deadline = deadline
        self.device, self.site = device, site
        self.writes = [
            ("PUT", f"/devices/{device}", SLEEP_TIME_PUT),
            ("PUT", f"/devices/{device}", SLEEP_PUT),
            (
                "POST",
                "/events",
                {
                    "event": {
                        "cgi": "modmaquina",
                        "option": "P1",
                        "value": str(1 - power),
                        "device_id": device,
                    }
                },
            ),
        ]
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
        or str(rows[0].get(identity)) != expected
    ):
        raise LabStopped("Scope mismatch")
    return rows[0]


async def measure(
    plan, request, check, wait, log, email, password, device, site, now=time.monotonic
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
        if (
            state["power"] not in {"0", "1"}
            or state["mode"] not in {"3", "8"}
            or state["cold_speed"] != "1"
            or state["scenary"] not in {"occupied", "sleep"}
            or not state["sleep_time"].isascii()
            or not state["sleep_time"].isdigit()
            or str(row.get("online")).lower() not in {"true", "1"}
        ):
            raise LabStopped("Unexpected device state")
        log(
            "rest",
            **{key: state[key] for key in state if key != "sleep_time"},
            duration_five=state["sleep_time"] == "5",
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
    require(await read(), "occupied", duration=False)
    await call(
        "duration", "PUT", path, params=formatted, body=SLEEP_TIME_PUT, structured=False
    )
    require(await read(), "occupied")
    await call("scene", "PUT", path, params=formatted, body=SLEEP_PUT, structured=False)
    start = now()
    log("t0")
    require(await read(), "sleep")
    offset = 240 if power == "0" else 120
    # Keep polling across both candidate expiry windows. Leave spacing before P1.
    for second in range(30, plan.observation_seconds + 1, 30):
        await wait(start + second)
        if second == offset:
            await call(
                "power",
                "POST",
                "/events",
                params=auth,
                body={
                    "event": {
                        "cgi": "modmaquina",
                        "device_id": device,
                        "option": "P1",
                        "value": str(1 - int(power)),
                    }
                },
                structured=False,
                due=start + second,
            )
            continue
        state = await read()
        if second < offset:
            require(state, "sleep")
        elif state["sleep_time"] != "5" or (power == "1" and state["power"] != "0"):
            raise LabStopped("Unexpected observation")
    if plan.observation_seconds % 30:
        raise LabStopped("Invalid observation schedule")
    log("observation_closed")


def local_input():
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
            if key in keys:
                if key in values or not value:
                    raise PermissionError("Invalid local configuration")
                values[key] = value
        email, password, device, site = (values[key] for key in keys)
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (device, site)):
            raise PermissionError("Invalid local scope")
        return email, password, device, site


async def run(plan, journal, email, password, device, site):
    loop = asyncio.get_running_loop()
    supervisor = LabSupervisor(auth_limit=1, state_limit=30)
    # Only this owned session uses the trial ceiling. Shared LAB defaults stay intact.
    supervisor.deadline = time.monotonic() + plan.hard_deadline_seconds
    policy = SleepPolicy(device, site, plan.required_start_power, supervisor.deadline)
    gateway = await create_gateway(policy, supervisor)
    gateway.protected.session._retry_connection = False
    started = time.monotonic()
    heartbeat = started

    def log(event, **fields):
        journal.write(
            json.dumps(
                {
                    "seconds": round(time.monotonic() - started, 3),
                    "event": event,
                    **fields,
                }
            )
            + "\n"
        )
        journal.flush()
        os.fsync(journal.fileno())

    task = asyncio.current_task()

    def stop():
        task.cancel()

    def operator():
        nonlocal heartbeat
        try:
            command = os.read(sys.stdin.fileno(), 4096).strip()
        except OSError:
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
        log("attempt", operation=label)
        async with gateway.request(
            method,
            BASE + path,
            params=params,
            json=body,
            headers={"x-requested-with": "XMLHttpRequest"},
            timeout=ClientTimeout(total=6),
        ) as response:
            log("http", operation=label, status=response.status)
            if not 200 <= response.status < 300:
                raise LabStopped("HTTP failure")
            return await response.json() if structured else None

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
                        plan, request, check, wait, log, email, password, device, site
                    )
        except (Exception, asyncio.CancelledError) as error:
            log("stopped", kind=type(error).__name__)
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
