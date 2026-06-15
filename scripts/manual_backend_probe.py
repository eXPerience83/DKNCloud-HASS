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
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .sanitize_backend_artifacts import sanitize_data, sanitize_text, sanitize_url
except ImportError:  # pragma: no cover - direct script execution fallback
    from sanitize_backend_artifacts import sanitize_data, sanitize_text, sanitize_url

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

RESTORE_FIELDS = (
    "mode",
    "cold_consign",
    "heat_consign",
    "cold_speed",
    "heat_speed",
    "min_temp_unoccupied",
    "max_temp_unoccupied",
    "scenary",
    "sleep_time",
    "power",
)


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


def _manual_fan_value(device: dict[str, Any]) -> str | None:
    try:
        available = int(device.get("availables_speeds", 0))
    except TypeError, ValueError:
        return None
    if available >= 2:
        return "2"
    if available >= 1:
        return "1"
    return None


def _runtime_event_payload(
    command: str,
    device_id: str,
    initial_device: dict[str, Any],
) -> dict[str, Any] | None:
    if command in {"cool_fan_speed_1", "heat_fan_speed_1"}:
        fan_value = _manual_fan_value(initial_device)
        if fan_value is None:
            return None
        option = "P3" if command == "cool_fan_speed_1" else "P4"
        return event_payload(device_id, option, fan_value)
    return command_payload(command, device_id)


def _runtime_expected_value(command: str, initial_device: dict[str, Any]) -> Any | None:
    if command in {"cool_fan_speed_1", "heat_fan_speed_1"}:
        return _manual_fan_value(initial_device)
    return COMMANDS[command].expected_value


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
    if cli_env_file is not None:
        path = cli_env_file if cli_env_file.is_absolute() else repo_root / cli_env_file
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    if values.get("DKN_ENV_FILE"):
        candidate = Path(values["DKN_ENV_FILE"])
        path = candidate if candidate.is_absolute() else repo_root / candidate
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    path = repo_root / "secrets" / "dkn.env"
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

    def _parse_body(self, raw: str) -> Any:
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {
                "parse_error": "non_json_response",
                "raw_text": sanitize_text(raw[:2000]),
            }

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
        except (URLError, TimeoutError, OSError) as err:
            return HttpResult(
                status=0,
                body={
                    "network_error": type(err).__name__,
                    "message": sanitize_text(str(err)),
                },
                url=url,
            )
        return HttpResult(status=status, body=self._parse_body(raw), url=url)

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


def _expected_matches(device: dict[str, Any], expected: dict[str, Any]) -> bool:
    for field, value in expected.items():
        if str(device.get(field)) != str(value):
            return False
    return True


def _timeline_entry(
    *,
    status: int,
    device: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"http_status": status}
    if device is not None:
        entry["device"] = sanitize_data(device)
    if error is not None:
        entry["error"] = sanitize_text(error)
    return entry


def wait_for_device_state(
    client: BackendClient,
    installation_id: str,
    device_id: str,
    expected: dict[str, Any],
    *,
    timeout_sec: int,
    poll_sec: int,
    sleep_func: Any = time.sleep,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Poll /devices until the selected device matches expected fields."""
    deadline = time.monotonic() + timeout_sec
    timeline: list[dict[str, Any]] = []
    last_device: dict[str, Any] | None = None
    saw_network_error = False

    while True:
        try:
            result = client.devices(installation_id)
            if result.status == 0:
                saw_network_error = True
                timeline.append(
                    _timeline_entry(
                        status=0, error=json.dumps(sanitize_data(result.body))
                    )
                )
            elif not 200 <= result.status < 300:
                timeline.append(
                    _timeline_entry(
                        status=result.status,
                        error=json.dumps(sanitize_data(result.body)),
                    )
                )
            else:
                last_device = _select_device(result.body, device_id)
                timeline.append(
                    _timeline_entry(status=result.status, device=last_device)
                )
                if _expected_matches(last_device, expected):
                    return "verified", last_device, timeline
        except (RuntimeError, KeyError, TypeError, ValueError) as err:
            timeline.append(_timeline_entry(status=0, error=str(err)))

        if time.monotonic() >= deadline:
            if saw_network_error and last_device is None:
                return "network_error", last_device, timeline
            if last_device is None:
                return "not_verified", last_device, timeline
            return "accepted_but_not_verified", last_device, timeline
        sleep_func(poll_sec)


def wait_for_mode_values(
    client: BackendClient,
    installation_id: str,
    device_id: str,
    *,
    final_values: set[str],
    alias_values: set[str],
    alias_status: str,
    timeout_sec: int,
    poll_sec: int,
    sleep_func: Any = time.sleep,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Poll mode and accept documented backend aliases."""
    deadline = time.monotonic() + timeout_sec
    timeline: list[dict[str, Any]] = []
    last_device: dict[str, Any] | None = None
    saw_alias = False

    while True:
        result = client.devices(installation_id)
        if result.status == 0:
            timeline.append(_timeline_entry(status=0, error=json.dumps(result.body)))
        elif 200 <= result.status < 300:
            last_device = _select_device(result.body, device_id)
            mode = str(last_device.get("mode"))
            timeline.append(_timeline_entry(status=result.status, device=last_device))
            if mode in final_values:
                return "verified", last_device, timeline
            if mode in alias_values:
                saw_alias = True
        else:
            timeline.append(
                _timeline_entry(
                    status=result.status,
                    error=json.dumps(sanitize_data(result.body)),
                )
            )

        if time.monotonic() >= deadline:
            if saw_alias:
                return alias_status, last_device, timeline
            return "accepted_but_not_verified", last_device, timeline
        sleep_func(poll_sec)


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
    print(
        json.dumps(
            sanitize_data(
                {
                    "dry_run": True,
                    "prepare_occupied": args.prepare_occupied,
                    "ensure_power_off_for_control_tests": (
                        args.ensure_power_off_for_control_tests
                    ),
                    "restore_all": args.restore_all,
                    "plan": plan,
                }
            ),
            indent=2,
        )
    )
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


def _is_control_command(command: str) -> bool:
    return COMMANDS[command].method == "POST"


def _verify_after_write(
    *,
    client: BackendClient,
    installation_id: str,
    device_id: str,
    expected: dict[str, Any],
    timeout_sec: int,
    poll_sec: int,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    return wait_for_device_state(
        client,
        installation_id,
        device_id,
        expected,
        timeout_sec=timeout_sec,
        poll_sec=poll_sec,
    )


def _prepare_control_state(
    *,
    client: BackendClient,
    installation_id: str,
    device_id: str,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if args.prepare_occupied:
        payload = scenary_payload("occupied")
        result = client.put_device(device_id, payload)
        status, observed, timeline = _verify_after_write(
            client=client,
            installation_id=installation_id,
            device_id=device_id,
            expected={"scenary": "occupied"},
            timeout_sec=args.verify_timeout_sec,
            poll_sec=args.verify_poll_sec,
        )
        records.append(
            {
                "command": "prepare_occupied",
                "http_status": result.status,
                "payload": sanitize_data(payload),
                "result": status if 200 <= result.status < 300 else "not_verified",
                "observed": sanitize_data(observed),
                "timeline": timeline,
            }
        )
    if args.ensure_power_off_for_control_tests:
        payload = event_payload(device_id, "P1", "0")
        result = client.event(payload)
        status, observed, timeline = _verify_after_write(
            client=client,
            installation_id=installation_id,
            device_id=device_id,
            expected={"power": "0"},
            timeout_sec=args.verify_timeout_sec,
            poll_sec=args.verify_poll_sec,
        )
        records.append(
            {
                "command": "prepare_power_off",
                "http_status": result.status,
                "payload": sanitize_data(payload),
                "result": status if 200 <= result.status < 300 else "not_verified",
                "observed": sanitize_data(observed),
                "timeline": timeline,
            }
        )
    return records


def _restore_payload_for_field(
    device_id: str,
    field: str,
    value: Any,
) -> tuple[str, dict[str, Any]] | None:
    event_options = {
        "mode": "P2",
        "cold_consign": "P7",
        "heat_consign": "P8",
        "cold_speed": "P3",
        "heat_speed": "P4",
        "power": "P1",
    }
    if field in event_options:
        return "POST", event_payload(device_id, event_options[field], str(value))
    if field == "scenary":
        return "PUT", scenary_payload(str(value))
    if field in {"sleep_time", "min_temp_unoccupied", "max_temp_unoccupied"}:
        return "PUT", root_payload(field, value)
    return None


def restore_initial_state(
    *,
    client: BackendClient,
    installation_id: str,
    device_id: str,
    initial_device: dict[str, Any],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """Best-effort final restoration of initial controllable state."""
    records: list[dict[str, Any]] = []
    for field in RESTORE_FIELDS:
        if field not in initial_device:
            continue
        restore_payload = _restore_payload_for_field(
            device_id,
            field,
            initial_device[field],
        )
        if restore_payload is None:
            continue
        method, payload = restore_payload
        try:
            result = (
                client.event(payload)
                if method == "POST"
                else client.put_device(device_id, payload)
            )
            status, observed, timeline = _verify_after_write(
                client=client,
                installation_id=installation_id,
                device_id=device_id,
                expected={field: initial_device[field]},
                timeout_sec=args.verify_timeout_sec,
                poll_sec=args.verify_poll_sec,
            )
            restore_result = status if 200 <= result.status < 300 else "restore_failed"
        except (RuntimeError, KeyError, TypeError, ValueError) as err:
            result = HttpResult(
                status=0,
                body={
                    "network_error": type(err).__name__,
                    "message": sanitize_text(str(err)),
                },
                url="",
            )
            observed = None
            timeline = []
            restore_result = "restore_failed"
        records.append(
            {
                "command": f"restore_{field}",
                "http_status": result.status,
                "payload": sanitize_data(payload),
                "expected": initial_device[field],
                "observed": sanitize_data(observed),
                "result": (
                    restore_result
                    if restore_result == "verified"
                    else f"restore_failed:{restore_result}"
                ),
                "timeline": timeline,
            }
        )
    return records


def _execute_command(
    *,
    client: BackendClient,
    installation_id: str,
    device_id: str,
    command: str,
    initial_device: dict[str, Any],
    output_dir: Path,
    flags: set[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    spec = COMMANDS[command]
    if spec.dangerous_flag and spec.dangerous_flag not in flags:
        return {
            "command": command,
            "result": "dangerous_skipped",
            "expected": spec.expected_value,
        }
    force_heatcool = command == "mode_heatcool_p2_4" and args.force_heatcool_auto_test
    if not force_heatcool and not _mode_supported(initial_device, spec.requires_mode):
        return {
            "command": command,
            "result": "skipped",
            "reason": f"mode {spec.requires_mode} not supported by modes bitmask",
        }

    payload = _runtime_event_payload(command, device_id, initial_device)
    if payload is None:
        return {
            "command": command,
            "result": "skipped",
            "reason": "no compatible fan speed or payload",
        }

    preparation = (
        _prepare_control_state(
            client=client,
            installation_id=installation_id,
            device_id=device_id,
            args=args,
        )
        if _is_control_command(command)
        else []
    )

    if spec.method == "PUT":
        result = client.put_device(device_id, payload)
    else:
        result = client.event(payload)
    expected_value = _runtime_expected_value(command, initial_device)

    if not 200 <= result.status < 300:
        verification_status = "network_error" if result.status == 0 else "not_verified"
        after_device = None
        timeline: list[dict[str, Any]] = []
    elif command == "mode_fan":
        verification_status, after_device, timeline = wait_for_mode_values(
            client,
            installation_id,
            device_id,
            final_values={"3"},
            alias_values={"8"},
            alias_status="accepted_alias_observed",
            timeout_sec=args.settle_timeout_sec,
            poll_sec=args.verify_poll_sec,
        )
    elif command == "mode_heatcool_p2_4":
        verification_status, after_device, timeline = wait_for_mode_values(
            client,
            installation_id,
            device_id,
            final_values={"4"},
            alias_values={"6", "7"},
            alias_status="accepted_heatcool_alias",
            timeout_sec=min(args.settle_timeout_sec, 60),
            poll_sec=args.verify_poll_sec,
        )
    elif spec.expected_field is not None and expected_value is not None:
        verification_status, after_device, timeline = _verify_after_write(
            client=client,
            installation_id=installation_id,
            device_id=device_id,
            expected={spec.expected_field: expected_value},
            timeout_sec=args.verify_timeout_sec,
            poll_sec=args.verify_poll_sec,
        )
    else:
        verification_status = "verified"
        after_device = None
        timeline = []

    record = {
        "command": command,
        "http_status": result.status,
        "request_url": sanitize_url(result.url),
        "payload": sanitize_data(payload),
        "expected": expected_value,
        "observed": _observed_value(after_device, spec.expected_field),
        "preparation": preparation,
        "result": verification_status,
        "timeline": timeline,
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
    should_restore = args.restore_all and any(
        command not in {"list", "snapshot"} for command in commands
    )
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
                args=args,
            )
        )
    if should_restore:
        restore_records = restore_initial_state(
            client=client,
            installation_id=installation_id,
            device_id=device_id,
            initial_device=initial_device,
            args=args,
        )
        summary.extend(restore_records)
        _write_json(output_dir / "restore_summary.json", restore_records)

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
    parser.add_argument("--force-heatcool-auto-test", action="store_true")
    parser.add_argument("--verify-timeout-sec", type=int, default=240)
    parser.add_argument("--verify-poll-sec", type=int, default=15)
    parser.add_argument("--settle-timeout-sec", type=int, default=90)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use short verification timings for quick local checks",
    )
    parser.add_argument(
        "--prepare-occupied",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Set scenary=occupied before /events control commands",
    )
    parser.add_argument(
        "--ensure-power-off-for-control-tests",
        action="store_true",
        help="Power off before control tests; this changes real device state",
    )
    parser.add_argument(
        "--restore-all",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Best-effort final restoration of the initial controllable state",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the manual backend probe CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.quick:
        args.verify_timeout_sec = min(args.verify_timeout_sec, 20)
        args.verify_poll_sec = min(args.verify_poll_sec, 2)
        args.settle_timeout_sec = min(args.settle_timeout_sec, 20)
    commands = _plan_commands(args)
    if args.dry_run:
        return dry_run(args, commands)
    return run_probe(args, commands)


if __name__ == "__main__":
    raise SystemExit(main())
