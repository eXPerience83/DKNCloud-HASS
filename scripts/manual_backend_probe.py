"""Local, sanitized DKN/Airzone Cloud backend probe."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .sanitize_backend_artifacts import sanitize_data, sanitize_url
except ImportError:  # pragma: no cover - direct script execution fallback
    from sanitize_backend_artifacts import sanitize_data, sanitize_url

BASE_URL = "https://dkn.airzonecloud.com"
DEFAULT_TIMEOUT = 30
SAFE_SUITE = [
    "snapshot",
    "scenary_occupied",
    "scenary_sleep",
    "scenary_vacant",
    "sleep_30",
    "sleep_40",
    "unoccupied_min_18",
    "unoccupied_max_28",
    "power_off",
    "power_on",
    "mode_cool",
    "mode_heat",
    "mode_fan",
    "mode_dry",
    "cool_setpoint_25",
    "heat_setpoint_21",
    "cool_fan_speed_1",
    "heat_fan_speed_1",
]

MODE_SUPPORT = {
    "mode_cool": 1,
    "mode_heat": 2,
    "mode_fan": 3,
    "mode_heatcool_p2_4": 4,
    "mode_dry": 5,
}


@dataclass(frozen=True)
class Credentials:
    """Backend credentials and optional target selection."""

    email: str | None
    password: str | None
    installation_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True)
class CommandSpec:
    """A backend command that can be executed by the probe."""

    name: str
    method: str
    path_template: str
    payload: dict[str, Any] | None
    expected_field: str | None = None
    expected_value: Any | None = None
    dangerous_flag: str | None = None
    requires_mode: int | None = None


def event_payload(device_id: str, option: str, value: str) -> dict[str, Any]:
    """Build a canonical /events payload."""
    return {
        "event": {
            "cgi": "modmaquina",
            "option": option,
            "value": value,
            "device_id": device_id,
        }
    }


def scenary_payload(value: str) -> dict[str, Any]:
    """Build a canonical scenary PUT payload."""
    return {"device": {"scenary": value}}


def root_payload(key: str, value: Any) -> dict[str, Any]:
    """Build a canonical root-level PUT payload."""
    return {key: value}


COMMANDS: dict[str, CommandSpec] = {
    "snapshot": CommandSpec("snapshot", "GET", "/devices", None),
    "scenary_occupied": CommandSpec(
        "scenary_occupied",
        "PUT",
        "/devices/{device_id}",
        scenary_payload("occupied"),
        "scenary",
        "occupied",
    ),
    "scenary_sleep": CommandSpec(
        "scenary_sleep",
        "PUT",
        "/devices/{device_id}",
        scenary_payload("sleep"),
        "scenary",
        "sleep",
    ),
    "scenary_vacant": CommandSpec(
        "scenary_vacant",
        "PUT",
        "/devices/{device_id}",
        scenary_payload("vacant"),
        "scenary",
        "vacant",
    ),
    "sleep_30": CommandSpec(
        "sleep_30",
        "PUT",
        "/devices/{device_id}",
        root_payload("sleep_time", 30),
        "sleep_time",
        30,
    ),
    "sleep_40": CommandSpec(
        "sleep_40",
        "PUT",
        "/devices/{device_id}",
        root_payload("sleep_time", 40),
        "sleep_time",
        40,
    ),
    "unoccupied_min_18": CommandSpec(
        "unoccupied_min_18",
        "PUT",
        "/devices/{device_id}",
        root_payload("min_temp_unoccupied", 18),
        "min_temp_unoccupied",
        18,
    ),
    "unoccupied_max_28": CommandSpec(
        "unoccupied_max_28",
        "PUT",
        "/devices/{device_id}",
        root_payload("max_temp_unoccupied", 28),
        "max_temp_unoccupied",
        28,
    ),
    "power_on": CommandSpec("power_on", "POST", "/events", None, "power", "1"),
    "power_off": CommandSpec("power_off", "POST", "/events", None, "power", "0"),
    "mode_cool": CommandSpec(
        "mode_cool", "POST", "/events", None, "mode", "1", requires_mode=1
    ),
    "mode_heat": CommandSpec(
        "mode_heat", "POST", "/events", None, "mode", "2", requires_mode=2
    ),
    "mode_fan": CommandSpec(
        "mode_fan", "POST", "/events", None, "mode", "3", requires_mode=3
    ),
    "mode_dry": CommandSpec(
        "mode_dry", "POST", "/events", None, "mode", "5", requires_mode=5
    ),
    "cool_setpoint_25": CommandSpec(
        "cool_setpoint_25", "POST", "/events", None, "cold_consign", "25.0"
    ),
    "heat_setpoint_21": CommandSpec(
        "heat_setpoint_21", "POST", "/events", None, "heat_consign", "21.0"
    ),
    "cool_fan_speed_1": CommandSpec(
        "cool_fan_speed_1", "POST", "/events", None, "cold_speed", "1"
    ),
    "heat_fan_speed_1": CommandSpec(
        "heat_fan_speed_1", "POST", "/events", None, "heat_speed", "1"
    ),
    "mode_heatcool_p2_4": CommandSpec(
        "mode_heatcool_p2_4",
        "POST",
        "/events",
        None,
        "mode",
        "4",
        "test_p2_4",
        requires_mode=4,
    ),
    "mode_alias_p2_8": CommandSpec(
        "mode_alias_p2_8",
        "POST",
        "/events",
        None,
        "mode",
        "8",
        "test_p2_8",
    ),
    "cool_auto_fan_p3_0": CommandSpec(
        "cool_auto_fan_p3_0",
        "POST",
        "/events",
        None,
        "cold_speed",
        "0",
        "test_auto_fan",
    ),
    "heat_auto_fan_p4_0": CommandSpec(
        "heat_auto_fan_p4_0",
        "POST",
        "/events",
        None,
        "heat_speed",
        "0",
        "test_auto_fan",
    ),
    "cool_setpoint_out_of_range": CommandSpec(
        "cool_setpoint_out_of_range",
        "POST",
        "/events",
        None,
        "cold_consign",
        "12.0",
        "test_out_of_range",
    ),
    "heat_setpoint_out_of_range": CommandSpec(
        "heat_setpoint_out_of_range",
        "POST",
        "/events",
        None,
        "heat_consign",
        "35.0",
        "test_out_of_range",
    ),
}

EVENT_MAP = {
    "power_on": ("P1", "1"),
    "power_off": ("P1", "0"),
    "mode_cool": ("P2", "1"),
    "mode_heat": ("P2", "2"),
    "mode_fan": ("P2", "3"),
    "mode_dry": ("P2", "5"),
    "cool_setpoint_25": ("P7", "25.0"),
    "heat_setpoint_21": ("P8", "21.0"),
    "cool_fan_speed_1": ("P3", "1"),
    "heat_fan_speed_1": ("P4", "1"),
    "mode_heatcool_p2_4": ("P2", "4"),
    "mode_alias_p2_8": ("P2", "8"),
    "cool_auto_fan_p3_0": ("P3", "0"),
    "heat_auto_fan_p4_0": ("P4", "0"),
    "cool_setpoint_out_of_range": ("P7", "12.0"),
    "heat_setpoint_out_of_range": ("P8", "35.0"),
}


def command_payload(command: str, device_id: str) -> dict[str, Any] | None:
    """Build the payload for a command and target device."""
    spec = COMMANDS[command]
    if spec.payload is not None:
        return spec.payload
    if command in EVENT_MAP:
        option, value = EVENT_MAP[command]
        return event_payload(device_id, option, value)
    return None


def is_dangerous_command(command: str) -> bool:
    """Return whether a command requires an explicit dangerous flag."""
    return COMMANDS[command].dangerous_flag is not None


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_env_file(
    repo_root: Path,
    *,
    cli_env_file: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Path | None:
    """Resolve the credential file using the documented precedence order."""
    values = os.environ if environ is None else environ
    candidates: list[Path] = []
    if cli_env_file is not None:
        candidates.append(cli_env_file)
    if values.get("DKN_ENV_FILE"):
        candidates.append(Path(values["DKN_ENV_FILE"]))
    candidates.extend(
        [
            repo_root / "secrets" / "dkn.env",
            repo_root / "secrets" / ".env",
        ]
    )
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else repo_root / candidate
        if path.exists():
            return path
    return None


def load_credentials(
    repo_root: Path,
    *,
    cli_env_file: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Credentials:
    """Load credentials from dkn.env-compatible files and environment overrides."""
    values = os.environ if environ is None else environ
    env_file = resolve_env_file(
        repo_root,
        cli_env_file=cli_env_file,
        environ=values,
    )
    file_values = _parse_env_file(env_file) if env_file is not None else {}
    merged_values = {**values, **file_values} if env_file is not None else dict(values)
    return Credentials(
        email=merged_values.get("AIRZONE_USERNAME") or merged_values.get("DKN_EMAIL"),
        password=merged_values.get("AIRZONE_PASSWORD")
        or merged_values.get("DKN_PASSWORD"),
        installation_id=merged_values.get("AIRZONE_INSTALLATION_ID"),
        device_id=merged_values.get("AIRZONE_DEVICE_ID"),
    )


@dataclass
class HttpResult:
    """HTTP response summary."""

    status: int
    body: Any
    url: str


class BackendClient:
    """Small stdlib HTTP client for the DKN/Airzone backend."""

    def __init__(
        self,
        *,
        email: str,
        password: str,
        base_url: str = BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.email = email
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> HttpResult:
        query_values = dict(query or {})
        if authenticated:
            if self.token is None:
                msg = "Client is not authenticated"
                raise RuntimeError(msg)
            query_values["user_email"] = self.email
            query_values["user_token"] = self.token
        url = f"{self.base_url}{path}"
        if query_values:
            url = f"{url}?{urlencode(query_values)}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            status = err.code
        parsed = json.loads(raw) if raw.strip() else None
        return HttpResult(status=status, body=parsed, url=url)

    def login(self) -> HttpResult:
        """Authenticate and store the returned user token."""
        result = self._json_request(
            "POST",
            "/users/sign_in",
            payload={"email": self.email, "password": self.password},
            authenticated=False,
        )
        token = _find_token(result.body)
        if result.status in {200, 201} and token:
            self.token = token
        return result

    def installations(self) -> HttpResult:
        """Fetch installation relations."""
        return self._json_request(
            "GET",
            "/installation_relations",
            query={"format": "json"},
        )

    def devices(self, installation_id: str) -> HttpResult:
        """Fetch the preferred devices snapshot."""
        return self._json_request(
            "GET",
            "/devices",
            query={"format": "json", "installation_id": installation_id},
        )

    def put_device(self, device_id: str, payload: dict[str, Any]) -> HttpResult:
        """Update a device via canonical PUT payloads."""
        return self._json_request(
            "PUT",
            f"/devices/{device_id}",
            query={"format": "json"},
            payload=payload,
        )

    def event(self, payload: dict[str, Any]) -> HttpResult:
        """Send a /events command."""
        return self._json_request("POST", "/events", payload=payload)


def _find_token(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("user_token", "authentication_token", "token"):
            token = value.get(key)
            if isinstance(token, str) and token:
                return token
        for item in value.values():
            token = _find_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = _find_token(item)
            if token:
                return token
    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("devices", "installation_relations", "installations"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return []


def _select_installation(relations: Any, requested_id: str | None) -> str:
    items = _as_list(relations)
    if requested_id:
        return requested_id
    for item in items:
        if isinstance(item, dict):
            installation_id = item.get("installation_id")
            if installation_id is not None:
                return str(installation_id)
    msg = "No installation_id found; pass --installation-id or AIRZONE_INSTALLATION_ID"
    raise RuntimeError(msg)


def _select_device(devices: Any, requested_id: str | None) -> dict[str, Any]:
    items = _as_list(devices)
    if requested_id:
        for item in items:
            if isinstance(item, dict) and str(item.get("id")) == requested_id:
                return item
        return {"id": requested_id}
    for item in items:
        if isinstance(item, dict) and item.get("id") is not None:
            return item
    msg = "No device id found; pass --device-id or AIRZONE_DEVICE_ID"
    raise RuntimeError(msg)


def _mode_supported(device: dict[str, Any], mode: int | None) -> bool:
    if mode is None:
        return True
    modes = str(device.get("modes", ""))
    if mode < 1 or mode > len(modes):
        return False
    return modes[mode - 1] == "1"


def _observed_value(device: dict[str, Any], field: str | None) -> Any | None:
    if field is None:
        return None
    value = device.get(field)
    return str(value) if value is not None else None


def _status_for_verification(
    http_status: int | None,
    expected: Any,
    observed: Any,
) -> str:
    if http_status is None:
        return "skipped"
    if not 200 <= http_status < 300:
        return "not_verified"
    if expected is None:
        return "verified"
    if str(expected) == str(observed):
        return "verified"
    return "accepted_but_not_verified"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run_dir(repo_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return repo_root / "secrets" / "manual-runs" / stamp


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sanitize_data(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _dangerous_flags(args: argparse.Namespace) -> set[str]:
    enabled: set[str] = set()
    if args.dangerous_probes:
        enabled.update(
            {
                "test_p2_4",
                "test_p2_8",
                "test_auto_fan",
                "test_out_of_range",
            }
        )
    for flag in ("test_p2_4", "test_p2_8", "test_auto_fan", "test_out_of_range"):
        if getattr(args, flag):
            enabled.add(flag)
    return enabled


def _plan_commands(args: argparse.Namespace) -> list[str]:
    commands: list[str] = []
    if args.list:
        commands.append("list")
    if args.snapshot:
        commands.append("snapshot")
    if args.safe_suite:
        commands.extend(SAFE_SUITE)
    if args.command:
        commands.append(args.command)
    if not commands:
        commands.append("snapshot")
    return commands


def dry_run(args: argparse.Namespace, commands: list[str]) -> int:
    """Print the sanitized command plan without touching the backend."""
    device_id = args.device_id or "DRY_RUN_DEVICE_ID"
    plan = []
    flags = _dangerous_flags(args)
    for command in commands:
        if command == "list":
            plan.append(
                {"command": command, "method": "GET", "path": "/installation_relations"}
            )
            continue
        spec = COMMANDS[command]
        if spec.dangerous_flag and spec.dangerous_flag not in flags:
            plan.append({"command": command, "result": "dangerous_skipped"})
            continue
        payload = command_payload(command, device_id)
        plan.append(
            {
                "command": command,
                "method": spec.method,
                "path": spec.path_template.format(device_id="<REDACTED_ID>"),
                "payload": sanitize_data(payload),
                "expected_field": spec.expected_field,
                "expected_value": spec.expected_value,
            }
        )
    print(json.dumps(sanitize_data({"dry_run": True, "plan": plan}), indent=2))
    return 0


def _login_client(credentials: Credentials) -> BackendClient:
    if not credentials.email or not credentials.password:
        msg = (
            "Missing AIRZONE_USERNAME/AIRZONE_PASSWORD in environment, "
            "secrets/dkn.env, or a file passed with --env-file"
        )
        raise RuntimeError(msg)
    client = BackendClient(email=credentials.email, password=credentials.password)
    result = client.login()
    if result.status not in {200, 201} or not client.token:
        msg = f"Login failed with HTTP {result.status}"
        raise RuntimeError(msg)
    return client


def _execute_list(
    client: BackendClient,
    output_dir: Path,
    requested_installation_id: str | None,
) -> dict[str, Any]:
    relations = client.installations()
    installation_id = _select_installation(relations.body, requested_installation_id)
    devices = client.devices(installation_id)
    _write_json(output_dir / "installations.json", relations.body)
    _write_json(output_dir / "devices.json", devices.body)
    return {
        "command": "list",
        "http_status": {"installations": relations.status, "devices": devices.status},
        "result": (
            "verified"
            if 200 <= relations.status < 300 and 200 <= devices.status < 300
            else "not_verified"
        ),
        "observed": {
            "installations": sanitize_data(relations.body),
            "devices": sanitize_data(devices.body),
        },
    }


def _execute_snapshot(
    client: BackendClient,
    installation_id: str,
    output_dir: Path,
    requested_device_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = client.devices(installation_id)
    _write_json(output_dir / "snapshot.json", snapshot.body)
    device = _select_device(snapshot.body, requested_device_id)
    return (
        {
            "command": "snapshot",
            "http_status": snapshot.status,
            "result": "verified" if 200 <= snapshot.status < 300 else "not_verified",
            "observed": sanitize_data(device),
        },
        device,
    )


def _restore_initial(
    client: BackendClient,
    command: str,
    device_id: str,
    initial_device: dict[str, Any],
) -> None:
    spec = COMMANDS[command]
    if spec.expected_field is None or spec.expected_field not in initial_device:
        return
    original = initial_device[spec.expected_field]
    if command.startswith("scenary_"):
        client.put_device(device_id, scenary_payload(str(original)))
        return
    if command.startswith(("sleep_", "unoccupied_")):
        client.put_device(device_id, root_payload(spec.expected_field, original))
        return
    restore_option = {
        "power": "P1",
        "mode": "P2",
        "cold_speed": "P3",
        "heat_speed": "P4",
        "cold_consign": "P7",
        "heat_consign": "P8",
    }.get(spec.expected_field)
    if restore_option:
        client.event(event_payload(device_id, restore_option, str(original)))


def _execute_command(
    *,
    client: BackendClient,
    installation_id: str,
    device_id: str,
    command: str,
    initial_device: dict[str, Any],
    output_dir: Path,
    flags: set[str],
) -> dict[str, Any]:
    spec = COMMANDS[command]
    if spec.dangerous_flag and spec.dangerous_flag not in flags:
        return {
            "command": command,
            "result": "dangerous_skipped",
            "expected": spec.expected_value,
        }
    if not _mode_supported(initial_device, spec.requires_mode):
        return {
            "command": command,
            "result": "skipped",
            "reason": f"mode {spec.requires_mode} not supported by modes bitmask",
        }

    payload = command_payload(command, device_id)
    if payload is None:
        return {"command": command, "result": "skipped", "reason": "no payload"}

    if spec.method == "PUT":
        result = client.put_device(device_id, payload)
    else:
        result = client.event(payload)
    time.sleep(2)
    after = client.devices(installation_id)
    after_device = _select_device(after.body, device_id)
    _restore_initial(client, command, device_id, initial_device)

    record = {
        "command": command,
        "http_status": result.status,
        "request_url": sanitize_url(result.url),
        "payload": sanitize_data(payload),
        "expected": spec.expected_value,
        "observed": _observed_value(after_device, spec.expected_field),
        "verification_status": after.status,
        "result": _status_for_verification(
            result.status,
            spec.expected_value,
            _observed_value(after_device, spec.expected_field),
        ),
    }
    _write_json(output_dir / f"{command}.json", record)
    return record


def run_probe(args: argparse.Namespace, commands: list[str]) -> int:
    """Run selected backend probes."""
    repo_root = _repo_root()
    credentials = load_credentials(repo_root, cli_env_file=args.env_file)
    if args.installation_id:
        credentials = Credentials(
            credentials.email,
            credentials.password,
            args.installation_id,
            credentials.device_id,
        )
    if args.device_id:
        credentials = Credentials(
            credentials.email,
            credentials.password,
            credentials.installation_id,
            args.device_id,
        )

    output_dir = args.output or _run_dir(repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = _login_client(credentials)
    summary: list[dict[str, Any]] = []
    if "list" in commands:
        summary.append(_execute_list(client, output_dir, credentials.installation_id))

    relations = client.installations()
    installation_id = _select_installation(relations.body, credentials.installation_id)
    initial_snapshot = client.devices(installation_id)
    initial_device = _select_device(initial_snapshot.body, credentials.device_id)
    device_id = str(initial_device["id"])
    _write_json(output_dir / "initial_snapshot.json", initial_snapshot.body)

    flags = _dangerous_flags(args)
    for command in commands:
        if command == "list":
            continue
        if command == "snapshot":
            snapshot_record, _device = _execute_snapshot(
                client,
                installation_id,
                output_dir,
                credentials.device_id,
            )
            summary.append(snapshot_record)
            continue
        summary.append(
            _execute_command(
                client=client,
                installation_id=installation_id,
                device_id=device_id,
                command=command,
                initial_device=initial_device,
                output_dir=output_dir,
                flags=flags,
            )
        )

    _write_json(output_dir / "summary.json", summary)
    print(json.dumps(sanitize_data(summary), indent=2))
    print(f"Sanitized run artifacts: {output_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the probe CLI parser."""
    parser = argparse.ArgumentParser(
        description="Run local sanitized DKN/Airzone Cloud backend probes.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    parser.add_argument(
        "--list", action="store_true", help="List installations/devices"
    )
    parser.add_argument(
        "--snapshot", action="store_true", help="Fetch selected snapshot"
    )
    parser.add_argument(
        "--safe-suite", action="store_true", help="Run conservative probes"
    )
    parser.add_argument(
        "--command",
        choices=sorted([*COMMANDS, "list"]),
        help="Run one command",
    )
    parser.add_argument("--installation-id", help="Override AIRZONE_INSTALLATION_ID")
    parser.add_argument("--device-id", help="Override AIRZONE_DEVICE_ID")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Credential file to load; defaults to DKN_ENV_FILE or secrets/dkn.env",
    )
    parser.add_argument("--output", type=Path, help="Sanitized output directory")
    parser.add_argument(
        "--dangerous-probes",
        action="store_true",
        help="Enable every dangerous/experimental probe",
    )
    parser.add_argument("--test-p2-4", action="store_true")
    parser.add_argument("--test-p2-8", action="store_true")
    parser.add_argument("--test-auto-fan", action="store_true")
    parser.add_argument("--test-out-of-range", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the manual backend probe CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = _plan_commands(args)
    if args.dry_run:
        return dry_run(args, commands)
    return run_probe(args, commands)


if __name__ == "__main__":
    raise SystemExit(main())
