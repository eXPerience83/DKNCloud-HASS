"""Run a separate HA daemon with disposable configuration and loopback-only I/O."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

parser = argparse.ArgumentParser()
parser.add_argument(
    "--protected", action="store_true", help="Run synthetic HTTPS/WSS experiment"
)
parser.add_argument(
    "--protected-real",
    action="store_true",
    help="Disabled pending independent acceptance and explicit authorization",
)
parser.add_argument(
    "--protected-case",
    choices=[
        "normal",
        "sleep",
        "duration",
        "quota",
        "authorization",
        "get",
        "cooldown",
        "redirect",
        "tls",
    ],
    default="normal",
)
args = parser.parse_args()
if args.protected_real:
    from scripts.protected_lab import REAL_DESTINATIONS_ENABLED

    if not REAL_DESTINATIONS_ENABLED:
        parser.error("Real destinations disabled")
    if args.protected_case != "normal":
        parser.error("Real laboratory does not accept synthetic scenarios")
    args.protected = True
component = "protected_ha_lab" if args.protected else "isolated_ha_lab"

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(
    prefix="dkn-ha-", dir=os.environ["TMPDIR"]
) as temporary:
    config = Path(temporary)
    components = config / "custom_components"
    components.mkdir()
    shutil.copytree(
        root / "custom_components/airzoneclouddaikin",
        components / "airzoneclouddaikin",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        root / "scripts" / component,
        components / component,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (components / component / "manifest.json").write_text(
        json.dumps(
            {
                "domain": component,
                "name": "Synthetic HA Lab",
                "version": "1.0.0",
                "requirements": [],
            }
        )
    )
    (config / "configuration.yaml").write_text(component + ":\n")
    environment = {
        "TMPDIR": os.environ["TMPDIR"],
        "PYTHONPATH": str(config) + os.pathsep + str(root),
        "DKN_LAB_TARGET": "real" if args.protected_real else "synthetic",
        "DKN_LAB_CASE": args.protected_case,
    }
    if args.protected_real:
        for key in (
            "DKN_EMAIL",
            "DKN_PASSWORD",
            "DKN_DEVICE_ID",
            "DKN_INSTALLATION_ID",
        ):
            environment[key] = os.environ[key]
    with (config / "process.log").open("w") as log:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/isolated_ha_process.py"),
                    str(config),
                    component,
                ],
                env=environment,
                stdout=subprocess.DEVNULL if args.protected else log,
                stderr=subprocess.DEVNULL if args.protected else log,
                timeout=300 if args.protected else 360,
            )
        except subprocess.TimeoutExpired:
            print("HA daemon exceeded the process deadline; child killed and awaited")
            print((config / "process.log").read_text()[-4000:])
            sys.exit(1)
    report = config / "lab-result.json"
    print(
        report.read_text()
        if report.exists()
        else "No lab report; daemon startup failed"
    )
    print("Process exit:", result.returncode)
    if not report.exists():
        print((config / "process.log").read_text()[-6000:])
        sys.exit(1)

    if args.protected:
        summary = json.loads(report.read_text())
        if args.protected_real:
            sys.exit(
                0
                if result.returncode == 0
                and not summary.get("error")
                and summary.get("loaded")
                and summary.get("push_applied")
                and summary.get("owned_tasks_closed")
                and summary.get("storage_files") == 0
                and summary.get("reason") == "duration"
                and summary.get("session_closed")
                else 1
            )
        expected = {
            "sleep": "forbidden_operation",
            "duration": "duration",
            "authorization": "duration",
            "get": "duration",
            "cooldown": "duration",
            "quota": "quota",
            "redirect": "http_security",
            "tls": "tls_verification",
        }
        if args.protected_case != "normal":
            sys.exit(
                0
                if result.returncode == 0
                and summary.get("reason") == expected[args.protected_case]
                and summary.get("backend_control_writes") == 0
                and summary.get("session_closed")
                and summary.get("server_closed")
                else 1
            )

        sys.exit(
            0
            if result.returncode == 0
            and not summary.get("error")
            and summary.get("push_applied")
            and summary.get("internal_write_blocked")
            and summary.get("reason") == "forbidden_operation"
            and summary.get("backend_control_writes") == 0
            and summary.get("session_closed")
            and summary.get("server_closed")
            else 1
        )

    if report.exists():
        summary = json.loads(report.read_text())
        if (
            result.returncode != 0
            or summary.get("error")
            or not summary.get("control_blocked")
            or not summary.get("migrated")
            or summary.get("push_power") != "0"
            or not summary.get("owned_tasks_closed")
            or summary.get("backend_control_writes") != 0
            or any(
                summary.get("platform_entities", {}).get(domain, 0) < 2
                for domain in {"climate", "switch", "number", "sensor", "binary_sensor"}
            )
            or summary.get("blocked_entity_controls", 0) < 4
            or len(summary.get("entity_controls_gated", {})) != 4
            or not all(summary.get("entity_controls_gated", {}).values())
            or summary.get("installations") != 2
            or summary.get("fallback_gets", 0) < 2
            or not summary.get("push_recovered")
            or not summary.get("fallback_spacing")
            or not summary.get("fallback_state_applied")
            or not summary.get("session_closed")
            or not summary.get("server_closed")
            or not summary.get("channel_403_isolated")
            or not summary.get("channel_recovered")
            or summary.get("healthy_climate_during_403") != "off"
        ):
            print((config / "process.log").read_text()[-6000:])
            sys.exit(1)
