"""Run a separate HA daemon with disposable configuration and loopback-only I/O."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
        root / "scripts/isolated_ha_lab",
        components / "isolated_ha_lab",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (components / "isolated_ha_lab/manifest.json").write_text(
        json.dumps(
            {
                "domain": "isolated_ha_lab",
                "name": "Synthetic HA Lab",
                "version": "1.0.0",
                "requirements": [],
            }
        )
    )
    (config / "configuration.yaml").write_text("isolated_ha_lab:\n")
    environment = dict(os.environ, PYTHONPATH=str(config) + os.pathsep + str(root))
    with (config / "process.log").open("w") as log:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/isolated_ha_process.py"),
                    str(config),
                ],
                env=environment,
                stdout=log,
                stderr=log,
                timeout=360,
            )
        except subprocess.TimeoutExpired:
            print(
                "HA daemon did not finish within 360 seconds; child killed and awaited"
            )
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

    if report.exists():
        summary = json.loads(report.read_text())
        if (
            summary.get("error")
            or not summary.get("control_blocked")
            or not summary.get("migrated")
            or summary.get("push_power") != "0"
            or not summary.get("owned_tasks_closed")
            or summary.get("backend_control_writes") != 0
            or summary.get("entities", 0) < 1
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
