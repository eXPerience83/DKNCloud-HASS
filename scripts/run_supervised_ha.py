"""Run the existing HA LAB with an expiring, private session grant.

Default execution is synthetic. Real modes require a separately approved grant;
controls additionally wait for an owner-approved numeric plan in the session dir.
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.session_grant import (  # noqa: E402
    claim_session,
    private_json,
    source_digest,
    validate_grant,
    write_private,
)


def tree_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=[
            "synthetic",
            "synthetic-observe",
            "synthetic-put",
            "synthetic-put-after-info",
            "synthetic-put-off",
            "observe",
            "put",
        ],
        default="synthetic-put",
    )
    parser.add_argument("--grant", type=Path)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--digest", action="store_true")
    parser.add_argument(
        "--field",
        choices=["scenary", "min_temp_unoccupied"],
        default="scenary",
        help="Only selects the local synthetic PUT field; real field is frozen in the grant",
    )
    args = parser.parse_args()
    if args.digest:
        print(source_digest(ROOT))
        return 0
    if args.mode == "synthetic":
        if args.grant or args.session_dir:
            parser.error("Synthetic execution does not accept a live grant")
        return subprocess.call(
            [
                sys.executable,
                str(ROOT / "scripts/run_isolated_ha.py"),
                "--protected",
                "--protected-case",
                "setpoint_trial",
            ]
        )
    fixture = args.mode in {
        "synthetic-observe",
        "synthetic-put",
        "synthetic-put-after-info",
        "synthetic-put-off",
    }
    if fixture:
        if args.grant or args.session_dir:
            parser.error("Synthetic session cannot use real inputs")
        fixture_directory = tempfile.TemporaryDirectory(
            prefix="dkn-fixture-journal-", dir=os.environ["TMPDIR"]
        )
        args.session_dir = Path(fixture_directory.name)
        grant = {
            "approved": True,
            "mode": "put" if args.mode.startswith("synthetic-put") else "observe",
            "nonce": secrets.token_hex(32),
            "source_digest": source_digest(ROOT),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "device_id": "device",
            "installation_id": "site",
        }
        if grant["mode"] == "put":
            grant["field"] = args.field
    else:
        if not args.grant or not args.session_dir or not os.environ.get("TMPDIR"):
            parser.error(
                "Real sessions require a private grant, session dir and TMPDIR"
            )
        grant = validate_grant(private_json(args.grant), ROOT)
        if grant["mode"] != args.mode:
            raise PermissionError("Session mode differs from owner grant")
    folder = claim_session(args.session_dir, grant)
    # Credentials are supplied by a private pipe, never argv, env or a stored copy.
    payload = (
        {"email": "synthetic@example.invalid", "password": "synthetic-password"}
        if fixture
        else json.loads(sys.stdin.readline())
    )
    if (
        type(payload) is not dict
        or set(payload) != {"email", "password"}
        or not all(isinstance(v, str) and v for v in payload.values())
    ):
        raise PermissionError("Missing private credentials")
    with tempfile.TemporaryDirectory(
        prefix="dkn-session-", dir=os.environ["TMPDIR"]
    ) as temporary:
        config = Path(temporary)
        config.chmod(0o700)
        components = config / "custom_components"
        components.mkdir()
        source = ROOT / "custom_components/airzoneclouddaikin"
        shutil.copytree(
            source,
            components / "airzoneclouddaikin",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        if tree_hashes(source) != tree_hashes(components / "airzoneclouddaikin"):
            raise PermissionError("Integration source copy changed")
        component = components / "protected_ha_lab"
        shutil.copytree(
            ROOT / "scripts/protected_ha_lab",
            component,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        write_private(
            component / "manifest.json",
            {
                "domain": "protected_ha_lab",
                "name": "Supervised LAB",
                "version": "1.0.0",
                "requirements": [],
            },
        )
        write_private(
            config / "session.json",
            {
                "grant": grant,
                "directory": str(folder.resolve()),
                "synthetic": fixture,
                "push_on_info": args.mode == "synthetic-put-after-info",
                "fixture_off": args.mode == "synthetic-put-off",
            },
        )
        environment = {
            "TMPDIR": os.environ["TMPDIR"],
            "PYTHONPATH": os.pathsep.join((str(config), str(ROOT))),
            "DKN_LAB_SESSION": str(config / "session.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        child = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/isolated_ha_process.py"),
                str(config),
                "protected_ha_lab",
            ],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        data = json.dumps(payload).encode() + b"\n"
        payload.clear()
        try:
            child.communicate(input=data, timeout=300)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
        except BaseException:
            child.kill()
            child.communicate()
            raise
        finally:
            del data
        report_path = config / "lab-result.json"
        report = (
            json.loads(report_path.read_text())
            if report_path.exists()
            else {"error": "no_report"}
        )
        report.update(
            process_exit=child.returncode,
            process_reaped=True,
            integration_byte_identical=True,
        )
    report["temporary_configuration_removed"] = not config.exists()
    write_private(folder / "result.json", report)
    print(json.dumps(report))
    return (
        0
        if (
            child.returncode == 0
            and not report.get("error")
            and report.get("session_closed")
            and report.get("pending_http_tasks") == 0
            and report.get("session_passed")
        )
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # No raw exceptions can expose private paths, input lines or URLs.
        print(json.dumps({"session_result": "failed", "error": type(error).__name__}))
        raise SystemExit(2) from None
