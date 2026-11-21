"""Private, expiring, single-use authorization for an isolated LAB session."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path


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
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise PermissionError("Session input must be a private regular file")
        value = json.load(stream)
    if type(value) is not dict:
        raise PermissionError("Invalid session input")
    return value


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


def authorize(path, directory, root, scenario, duration, scope):
    """Consume a fresh scenario-specific grant before opening a transport."""
    grant = private_json(path)
    nonce = grant["nonce"]
    expiry = datetime.fromisoformat(grant["expires_at"])
    if (
        set(grant)
        != {
            "nonce",
            "expires_at",
            "source_digest",
            "approved",
            "scenario",
            "device_id",
            "installation_id",
        }
        or grant["approved"] is not True
        or grant["scenario"] != scenario
        or grant["source_digest"] != source_digest(root)
        or (grant["device_id"], grant["installation_id"]) != scope
        or not duration <= (expiry - datetime.now(UTC)).total_seconds() <= 1800
        or not isinstance(nonce, str)
        or len(nonce) != 64
        or any(ch not in "0123456789abcdef" for ch in nonce)
    ):
        raise PermissionError("Invalid Sleep authorization")
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise PermissionError("Private session directory required")
    # A common lock covers both experiments. A crash leaves it in place.
    lock = directory / "active"
    lock.mkdir(mode=0o700)
    try:
        folder = directory / hashlib.sha256(nonce.encode()).hexdigest()
        folder.mkdir(mode=0o700)
        write_private(folder / "claim.json", {"scenario": scenario})
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        lock.rmdir()
        raise
    return folder, lock
