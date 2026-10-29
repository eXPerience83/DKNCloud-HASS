"""Capture allowlisted manual observations, without credentials or cloud requests."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "secrets" / "manual-runs"
CASES = (
    "setup",
    "registry",
    "state_read",
    "ha_command",
    "official_app",
    "physical_controls",
    "refresh_102",
    "reload",
    "restart",
    "offline_recovery",
    "reauth",
    "options",
    "marketplace_install",
    "hacs_upgrade",
)
CHECKPOINTS = (
    "baseline",
    "action",
    "snapshot_before",
    "refresh_requested",
    "snapshot_after",
    "ha_observed",
    "recovered",
    "verification",
)
SOURCES = (
    "home_assistant",
    "official_app",
    "physical_unit",
    "backend_snapshot",
    "tester",
)
RESULTS = ("pass", "fail", "pending", "stale", "unavailable")
MODES = ("unknown", "off", "cool", "heat", "fan_only", "dry", "heat_cool")
POWER = ("unknown", "on", "off")
FAN = ("unknown", "auto", "1", "2", "3", "4", "5")
VARIANTS = ("main", "pr102")
RUN_NAME = re.compile(r"dkn-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
HA_VERSION = re.compile(r"20[0-9]{2}\.[0-9]{1,2}\.[0-9]{1,2}(?:b[0-9]+|rc[0-9]+)?\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
RECORD_KEYS = {
    "timestamp_utc",
    "case",
    "checkpoint",
    "source",
    "result",
    "elapsed_seconds",
    "power",
    "mode",
    "fan",
    "setpoint_c",
}


def _run_dir(name: str) -> Path:
    if not RUN_NAME.fullmatch(name):
        raise ValueError("Use the generated run name, not a filesystem path")
    return RUNS / name


def _validate_record(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != RECORD_KEYS:
        raise ValueError("Unexpected fields in a manual observation")
    choices = {
        "case": CASES,
        "checkpoint": CHECKPOINTS,
        "source": SOURCES,
        "result": RESULTS,
        "power": POWER,
        "mode": MODES,
        "fan": FAN,
    }
    for key, allowed in choices.items():
        if value[key] not in allowed:
            raise ValueError(f"Invalid allowlisted value for {key}")
    try:
        datetime.strptime(value["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid observation timestamp") from error
    elapsed = value["elapsed_seconds"]
    if elapsed is not None and (
        type(elapsed) not in (float, int) or not 0 <= elapsed <= 3600
    ):
        raise ValueError("Elapsed seconds must be between 0 and 3600")
    temp = value["setpoint_c"]
    if temp is not None and (type(temp) not in (float, int) or not 0 <= temp <= 50):
        raise ValueError("Setpoint must be between 0 and 50 degrees Celsius")
    return value


def start(variant: str, ha_version: str, revision: str | None = None) -> str:
    if variant not in VARIANTS or not HA_VERSION.fullmatch(ha_version):
        raise ValueError("Invalid variant or HA version")
    if revision is not None and not REVISION.fullmatch(revision):
        raise ValueError("Revision must be a 40-character lowercase Git SHA")
    RUNS.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    while True:
        name = f"dkn-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
        folder = _run_dir(name)
        try:
            folder.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    metadata = {
        "schema": 1,
        "variant": variant,
        "ha_version": ha_version,
        "revision": revision,
        "started_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evidence_kind": "tester_recorded",
    }
    (folder / "session.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    (folder / "observations.jsonl").touch(mode=0o600)
    return name


def record(
    name: str,
    case: str,
    checkpoint: str,
    source: str,
    result: str,
    elapsed_seconds: float | None = None,
    power: str = "unknown",
    mode: str = "unknown",
    fan: str = "unknown",
    setpoint_c: float | None = None,
) -> None:
    folder = _run_dir(name)
    if not (folder / "session.json").is_file() or (folder / "evidence.zip").exists():
        raise ValueError("Run does not exist or has already been finalized")
    item = _validate_record(
        {
            "timestamp_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "case": case,
            "checkpoint": checkpoint,
            "source": source,
            "result": result,
            "elapsed_seconds": elapsed_seconds,
            "power": power,
            "mode": mode,
            "fan": fan,
            "setpoint_c": setpoint_c,
        }
    )
    with (folder / "observations.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(item, sort_keys=True) + "\n")


def finish(name: str) -> Path:
    folder = _run_dir(name)
    metadata = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    allowed = {
        "schema",
        "variant",
        "ha_version",
        "revision",
        "started_utc",
        "evidence_kind",
    }
    if set(metadata) != allowed:
        raise ValueError("Unexpected session metadata; refusing export")
    if (
        metadata["schema"] != 1
        or metadata["variant"] not in VARIANTS
        or not HA_VERSION.fullmatch(metadata["ha_version"])
    ):
        raise ValueError("Invalid session metadata")
    try:
        datetime.strptime(metadata["started_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid session timestamp") from error
    revision = metadata["revision"]
    if revision is not None and (
        not isinstance(revision, str) or not REVISION.fullmatch(revision)
    ):
        raise ValueError("Invalid revision")
    if metadata["evidence_kind"] != "tester_recorded":
        raise ValueError("Evidence type must be tester-recorded observations")
    records = []
    with (folder / "observations.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(_validate_record(json.loads(line)))
    if not records:
        raise ValueError("No observations recorded")
    counts = {
        status: sum(item["result"] == status for item in records) for status in RESULTS
    }
    summary = [
        "# DKN real-device manual observation log",
        "",
        f"- Variant: {metadata['variant']}",
        f"- Home Assistant: {metadata['ha_version']}",
        f"- Revision: {revision or 'not recorded'}",
        f"- Recorded checkpoints: {len(records)}",
        "",
        "This is tester-entered evidence, NOT automated backend verification.",
        "Do not mark a scenario passed without observing it.",
        "",
        "## Results",
        "",
        *[f"- {status}: {count}" for status, count in counts.items()],
        "",
        "## Checkpoints",
        "",
        "| Case | Checkpoint | Source | Result | Elapsed (s) |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for item in records:
        duration = item["elapsed_seconds"]
        summary.append(
            f"| {item['case']} | {item['checkpoint']} | {item['source']} | "
            f"{item['result']} | {duration if duration is not None else '-'} |"
        )
    contents = {
        "session.json": json.dumps(metadata, indent=2) + "\n",
        "observations.jsonl": "".join(
            json.dumps(item, sort_keys=True) + "\n" for item in records
        ),
        "SUMMARY.md": "\n".join(summary) + "\n",
    }
    destination = folder / "evidence.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, data in contents.items():
            archive.writestr(filename, data)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    initial = commands.add_parser("start")
    initial.add_argument("--variant", choices=VARIANTS, required=True)
    initial.add_argument("--ha-version", required=True)
    initial.add_argument("--revision")
    observation = commands.add_parser("record")
    observation.add_argument("--run", required=True)
    observation.add_argument("--case", choices=CASES, required=True)
    observation.add_argument("--checkpoint", choices=CHECKPOINTS, required=True)
    observation.add_argument("--source", choices=SOURCES, required=True)
    observation.add_argument("--result", choices=RESULTS, required=True)
    observation.add_argument("--elapsed-seconds", type=float)
    observation.add_argument("--power", choices=POWER, default="unknown")
    observation.add_argument("--mode", choices=MODES, default="unknown")
    observation.add_argument("--fan", choices=FAN, default="unknown")
    observation.add_argument("--setpoint-c", type=float)
    ending = commands.add_parser("finish")
    ending.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "start":
            print(start(args.variant, args.ha_version, args.revision))
        elif args.action == "record":
            record(
                args.run,
                args.case,
                args.checkpoint,
                args.source,
                args.result,
                args.elapsed_seconds,
                args.power,
                args.mode,
                args.fan,
                args.setpoint_c,
            )
            print("Observation recorded (allowlisted fields only)")
        else:
            print(f"Local evidence ZIP: {finish(args.run)}")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"Cannot record or export manual evidence: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
