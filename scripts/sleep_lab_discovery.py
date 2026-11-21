"""Read-only account discovery for the Sleep LAB. No device writes."""

import asyncio
import json
import re
from datetime import UTC, datetime

from aiohttp import ClientTimeout

from custom_components.airzoneclouddaikin import _is_online
from scripts.lab_policy import ReadOnlyPolicy
from scripts.protected_lab import (
    GuardedContext,
    LabStopped,
    LabSupervisor,
    create_gateway,
)
from scripts.sleep_lab_grant import write_private
from scripts.sleep_lab_run import BASE, ROOT, fail_on_disconnect, local_input

SCOPE = ROOT / "secrets/sleep-scope.json"


class DiscoveryPolicy(ReadOnlyPolicy):
    def __init__(self):
        super().__init__({"discovery"}, set(), "", enabled=True)

    def validate(self, method, url, *, params=None, json=None, data=None):
        if (method, url) == ("POST", BASE + "/users/sign_in"):
            return super().validate(method, url, params=params, json=json, data=data)
        path = url.removeprefix(BASE)
        required = {"user_email", "user_token", "format"}
        if path == "/devices":
            required.add("installation_id")
        if (
            method != "GET"
            or url != BASE + path
            or path not in {"/devices", "/installation_relations"}
            or type(params) is not dict
            or set(params) != required
        ):
            raise PermissionError("Discovery operation denied")
        super().validate(method, url, params=params, json=json, data=data)

    def validate_socket(self, url):
        raise PermissionError("Discovery permits REST only")


def identity(row, key):
    value = row.get(key)
    if type(value) not in {str, int} or not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
        raise LabStopped("Invalid discovery identity")
    return str(value)


def rows(value, wrapper):
    result = value.get(wrapper) if isinstance(value, dict) else value
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise LabStopped("Invalid discovery response")
    return result


def summary(row):
    fields = {}
    for key, allowed in {
        "power": {"0", "1"},
        "scenary": {"occupied", "vacant", "sleep"},
        "mode": set("12345678"),
        "cold_speed": set("0123456789"),
        "sleep_time": {str(number) for number in range(1441)},
        "availables_speeds": set("0123456789"),
    }.items():
        value = str(row.get(key))
        fields[key] = value if value in allowed else None
    modes = str(row.get("modes", ""))
    fields["p2_3_supported"] = (
        modes[2] == "1" if re.fullmatch("[01]{3,8}", modes) else None
    )
    fields["online_by_connection_date"] = _is_online(row, datetime.now(UTC))
    return fields


async def discover(email, password):
    owner = LabSupervisor(duration=120, auth_limit=1, state_limit=9)
    policy = DiscoveryPolicy()
    gateway = await create_gateway(policy, owner, middlewares=(fail_on_disconnect,))

    async def read(method, path, *, params=None, body=None):
        url = BASE + path
        # Discovery must inspect all returned identities before any target exists.
        # Leave the measurement gateway's singleton response check unchanged.
        gateway.protected.validate_request(method, url, params=params, json=body)
        async with GuardedContext(
            gateway,
            lambda: gateway.protected.request(
                method, url, params=params, json=body, timeout=ClientTimeout(total=6)
            ),
            "auth" if method == "POST" else "state",
        ) as response:
            if not 200 <= response.status < 300:
                raise LabStopped(f"Discovery HTTP {response.status}")
            return await response.json()

    async with gateway.protected.session:
        with gateway.enforce():
            login = await read(
                "POST", "/users/sign_in", body={"email": email, "password": password}
            )
            token = login.get("authentication_token") or login.get("user", {}).get(
                "authentication_token"
            )
            if not isinstance(token, str) or not token:
                raise LabStopped("Login unconfirmed")
            auth = {"user_email": email, "user_token": token, "format": "json"}
            installations = rows(
                await read("GET", "/installation_relations", params=auth),
                "installation_relations",
            )
            sites = {identity(row, "installation_id") for row in installations}
            if not 0 < len(sites) <= 8:
                raise LabStopped("Installation count outside discovery budget")
            policy.installations = frozenset(sites)
            devices = []
            for site in sorted(sites):
                value = await read(
                    "GET", "/devices", params=auth | {"installation_id": site}
                )
                devices.extend(
                    (site, identity(row, "id"), row) for row in rows(value, "devices")
                )
    return sites, devices


def main():
    try:
        email, password = local_input(require_scope=False)
        sites, devices = asyncio.run(discover(email, password))
        if len(devices) == 1:
            site, device, _ = devices[0]
            write_private(SCOPE, {"device_id": device, "installation_id": site})
        print(
            json.dumps(
                {
                    "installations": len(sites),
                    "devices": len(devices),
                    "candidates": [
                        summary(row)
                        | (
                            {"label": str(row.get("name", ""))[:60]}
                            if len(devices) > 1
                            else {}
                        )
                        for _, _, row in devices
                    ],
                    "scope_saved": len(devices) == 1,
                },
                ensure_ascii=True,
            )
        )
    except (Exception, KeyboardInterrupt) as error:
        print(f"Discovery stopped ({type(error).__name__}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
