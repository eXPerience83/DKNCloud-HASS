"""Private, expiring, single-use authorization for an isolated LAB session."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from scripts.lab_policy import ReadOnlyPolicy


def source_digest(root: Path) -> str:
    """Bind approval to runtime, harness and dependency contract, not git metadata."""
    digest = hashlib.sha256()
    paths = [
        *(
            p
            for p in root.glob("custom_components/airzoneclouddaikin/**/*")
            if p.is_file()
        ),
        *root.glob("scripts/**/*.py"),
        root / "pyproject.toml",
        root / "uv.lock",
    ]
    for path in sorted(paths):
        if "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def private_json(path: Path) -> dict:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise PermissionError("Session input must be a private regular file")
    value = json.loads(path.read_text())
    if type(value) is not dict:
        raise PermissionError("Invalid session input")
    return value


def validate_grant(value: dict, root: Path) -> dict:
    """Run before credential access and again inside the HA child."""
    if set(value) != (
        {
            "approved",
            "mode",
            "nonce",
            "source_digest",
            "expires_at",
            "device_id",
            "installation_id",
        }
        | ({"field"} if value.get("mode") == "put" else set())
    ):
        raise PermissionError("Invalid session grant fields")
    if (
        value["approved"] is not True
        or value["mode"] not in {"observe", "put"}
        or not isinstance(value["nonce"], str)
        or not 32 <= len(value["nonce"]) <= 256
        or value["source_digest"] != source_digest(root)
    ):
        raise PermissionError("Session grant not approved for this source")
    expiry = datetime.fromisoformat(value["expires_at"])
    remaining = (expiry - datetime.now(UTC)).total_seconds()
    if not 0 < remaining <= 3600:
        raise PermissionError("Session grant expired or too long")
    scope = (value["device_id"], value["installation_id"])
    if value["mode"] == "put" and value["field"] not in {
        "scenary",
        "min_temp_unoccupied",
    }:
        raise PermissionError("PUT grant field denied")
    if not (
        all(isinstance(x, str) and x for x in scope)
        or (value["mode"] == "observe" and scope == (None, None))
    ):
        raise PermissionError("Control requires an exact private device scope")
    return dict(value)


def claim_session(directory: Path, grant: dict) -> Path:
    """Consume permission durably before login, including failed launches."""
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise PermissionError("Session directory must be private")
    name = hashlib.sha256(grant["nonce"].encode()).hexdigest()
    folder = directory / name
    folder.mkdir(mode=0o700)  # Exclusive: crashes never refund permission.
    write_private(folder / "claim.json", grant)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return folder


def claim_child(directory: Path, grant: dict) -> None:
    """An accidentally restarted HA child cannot reuse its parent's grant."""
    if private_json(directory / "claim.json") != grant:
        raise PermissionError("Missing durable session claim")
    fd = os.open(
        directory / "child.claimed", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, value: dict) -> None:
    """Atomic private state, never a credential or raw backend payload."""
    temporary = path.with_suffix(".pending")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = json.dumps(value, sort_keys=True).encode()
        if os.write(fd, data) != len(data):
            raise OSError("Incomplete session state")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def approval_values(grant: dict, plan) -> dict:
    return {
        "field": plan.field,
        "baseline_power": getattr(plan, "power", "1"),
        "baseline_mode": plan.mode,
        "nonce": grant["nonce"],
        "source_digest": grant["source_digest"],
        "device_id": plan.device_id,
        "installation_id": plan.installation_id,
        "option": plan.option,
        "original": plan.original,
        "modified": plan.modified,
        "minimum": str(plan.minimum) if plan.minimum is not None else None,
        "maximum": str(plan.maximum) if plan.maximum is not None else None,
    }


def check_plan_approval(path: Path, grant: dict, plan) -> bool:
    if not path.exists():
        return False
    value = private_json(path)
    if value != {**approval_values(grant, plan), "approved": True}:
        raise PermissionError("Owner approval differs from exact numeric plan")
    return True


class ScopedDiscoveryPolicy(ReadOnlyPolicy):
    """Reuse private LAB single-account discovery, with optional exact scope."""

    def __init__(self, app_key, grant):
        super().__init__(
            {grant["device_id"] or "unselected"},
            {grant["installation_id"]} if grant["installation_id"] else set(),
            app_key,
            enabled=True,
        )
        self.selected = False

    def observe(self, path, value):
        if path not in {"/devices", "/installation_relations"}:
            return
        key = "devices" if path == "/devices" else "installation_relations"
        rows = value.get(key) if isinstance(value, dict) else value
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            raise PermissionError("Unambiguous single-device account required")
        row = rows[0]
        identity = row.get("id") if key == "devices" else row.get("installation_id")
        if isinstance(identity, bool) or not isinstance(identity, (str, int)):
            raise PermissionError("Invalid session scope")
        scope = {str(identity)}
        if key == "devices":
            if self.devices != {"unselected"} and self.devices != scope:
                raise PermissionError("Device scope changed")
            self.devices, self.selected = frozenset(scope), True
        else:
            if self.installations and self.installations != scope:
                raise PermissionError("Installation scope changed")
            self.installations = frozenset(scope)

    def validate(self, method, url, **kwargs):
        from yarl import URL

        path = URL(url).path
        if path in {"/events", "/pusher/auth"} and not self.selected:
            raise PermissionError("Device not selected")
        if (
            path == "/devices"
            and str((kwargs.get("params") or {}).get("installation_id"))
            not in self.installations
        ):
            raise PermissionError("Installation not selected")
        return super().validate(method, url, **kwargs)
