"""Tests for local backend probe and sanitizer helpers."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import (
    manual_backend_probe as probe,
    sanitize_backend_artifacts as sanitizer,
)


def _write_env_file(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"AIRZONE_USERNAME={marker}@example.test",
                f"AIRZONE_PASSWORD={marker}-password",
                f"AIRZONE_INSTALLATION_ID={marker}-installation",
                f"AIRZONE_DEVICE_ID={marker}-device",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_env_file_cli_path_wins(tmp_path: Path) -> None:
    """--env-file should win over every other credential file source."""
    repo_root = tmp_path
    _write_env_file(repo_root / "secrets" / "dkn.env", "default")
    _write_env_file(repo_root / "from-env.env", "env")
    _write_env_file(repo_root / "from-cli.env", "cli")

    credentials = probe.load_credentials(
        repo_root,
        cli_env_file=Path("from-cli.env"),
        environ={"DKN_ENV_FILE": "from-env.env"},
    )

    assert credentials == probe.Credentials(
        email="cli@example.test",
        password="cli-password",
        installation_id="cli-installation",
        device_id="cli-device",
    )


def test_env_file_environment_path_wins_over_default(tmp_path: Path) -> None:
    """DKN_ENV_FILE should win over secrets/dkn.env."""
    repo_root = tmp_path
    _write_env_file(repo_root / "secrets" / "dkn.env", "default")
    _write_env_file(repo_root / "from-env.env", "env")

    credentials = probe.load_credentials(
        repo_root,
        environ={"DKN_ENV_FILE": "from-env.env"},
    )

    assert credentials.email == "env@example.test"
    assert credentials.password == "env-password"


def test_dkn_env_works_as_default(tmp_path: Path) -> None:
    """secrets/dkn.env should be the default credential file."""
    repo_root = tmp_path
    _write_env_file(repo_root / "secrets" / "dkn.env", "dkn")

    credentials = probe.load_credentials(repo_root, environ={})

    assert credentials.email == "dkn@example.test"
    assert credentials.password == "dkn-password"


def test_legacy_dot_env_file_is_ignored(tmp_path: Path) -> None:
    """secrets/.env should not be used as a credential fallback."""
    repo_root = tmp_path
    _write_env_file(repo_root / "secrets" / ".env", "legacy")

    credentials = probe.load_credentials(repo_root, environ={})

    assert credentials == probe.Credentials(
        email=None,
        password=None,
        installation_id=None,
        device_id=None,
    )


def test_direct_environment_credentials_work_without_files(tmp_path: Path) -> None:
    """Direct AIRZONE_* environment values should work when no file exists."""
    credentials = probe.load_credentials(
        tmp_path,
        environ={
            "AIRZONE_USERNAME": "envdirect@example.test",
            "AIRZONE_PASSWORD": "envdirect-password",
            "AIRZONE_INSTALLATION_ID": "envdirect-installation",
            "AIRZONE_DEVICE_ID": "envdirect-device",
        },
    )

    assert credentials == probe.Credentials(
        email="envdirect@example.test",
        password="envdirect-password",
        installation_id="envdirect-installation",
        device_id="envdirect-device",
    )


def test_sanitize_email_token_and_password() -> None:
    """Sanitizer should redact common credential material."""
    payload = {
        "email": "owner@example.com",
        "password": "super-secret",
        "user_token": "token-123",
        "nested": {"authentication_token": "auth-456"},
    }

    sanitized = sanitizer.sanitize_data(payload)

    assert sanitized == {
        "email": "<REDACTED_EMAIL>",
        "password": "<REDACTED_PASSWORD>",
        "user_token": "<REDACTED_TOKEN>",
        "nested": {"authentication_token": "<REDACTED_TOKEN>"},
    }


def test_sanitize_sensitive_url_query_params() -> None:
    """Sensitive query params and device URL path ids should be redacted."""
    url = (
        "https://dkn.airzonecloud.com/devices/device-real-id?"
        "format=json&user_email=owner@example.com&user_token=token-123"
    )

    sanitized = sanitizer.sanitize_url(url)

    assert "owner@example.com" not in sanitized
    assert "token-123" not in sanitized
    assert "device-real-id" not in sanitized
    assert "user_email=%3CREDACTED_EMAIL%3E" in sanitized
    assert "/devices/<REDACTED_ID>" in sanitized


def test_sanitize_ids_mac_pin_and_names() -> None:
    """Backend identifiers and user/device labels should not survive."""
    payload = {
        "id": "relation-1",
        "installation_id": "installation-1",
        "device_id": "device-1",
        "mac": "AA:BB:CC:DD:EE:FF",
        "pin": "1234",
        "name": "Living Room",
        "address": "Main Street",
    }

    sanitized = sanitizer.sanitize_data(payload)

    assert sanitized == {
        "id": "<REDACTED_ID>",
        "installation_id": "<REDACTED_ID>",
        "device_id": "<REDACTED_ID>",
        "mac": "<REDACTED_MAC>",
        "pin": "<REDACTED_PIN>",
        "name": "<REDACTED_NAME>",
        "address": "<REDACTED_LOCATION>",
    }


def test_extract_technical_snapshot_fields() -> None:
    """Technical device state should be kept while identifiers are redacted."""
    snapshots = sanitizer.extract_technical_snapshots(
        {
            "devices": [
                {
                    "id": "device-real-id",
                    "name": "Bedroom",
                    "power": "1",
                    "mode": "2",
                    "modes": "11111",
                    "scenary": "occupied",
                    "sleep_time": 30,
                    "mac": "AA:BB:CC:DD:EE:FF",
                }
            ]
        }
    )

    assert snapshots == [
        {
            "power": "1",
            "mode": "2",
            "modes": "11111",
            "scenary": "occupied",
            "sleep_time": 30,
            "device_id": "<REDACTED_ID>",
        }
    ]


@pytest.mark.parametrize(
    ("command", "option", "value"),
    [
        ("power_on", "P1", "1"),
        ("mode_cool", "P2", "1"),
        ("cool_fan_speed_1", "P3", "1"),
        ("heat_fan_speed_1", "P4", "1"),
        ("cool_setpoint_25", "P7", "25.0"),
        ("heat_setpoint_21", "P8", "21.0"),
    ],
)
def test_event_payload_shapes(command: str, option: str, value: str) -> None:
    """Event commands should use the documented P-code payload shape."""
    payload = probe.command_payload(command, "device-1")

    assert payload == {
        "event": {
            "cgi": "modmaquina",
            "option": option,
            "value": value,
            "device_id": "device-1",
        }
    }


def test_device_put_payload_shapes() -> None:
    """PUT device commands should use canonical nested/root-level shapes."""
    assert probe.command_payload("scenary_sleep", "device-1") == {
        "device": {"scenary": "sleep"}
    }
    assert probe.command_payload("sleep_30", "device-1") == {"sleep_time": 30}
    assert probe.command_payload("unoccupied_min_18", "device-1") == {
        "min_temp_unoccupied": 18
    }
    assert probe.command_payload("unoccupied_max_28", "device-1") == {
        "max_temp_unoccupied": 28
    }


def test_dangerous_command_classification() -> None:
    """Experimental backend probes should require explicit flags."""
    assert probe.is_dangerous_command("mode_heatcool_p2_4")
    assert probe.is_dangerous_command("mode_alias_p2_8")
    assert probe.is_dangerous_command("cool_auto_fan_p3_0")
    assert probe.is_dangerous_command("heat_auto_fan_p4_0")
    assert probe.is_dangerous_command("cool_setpoint_out_of_range")
    assert not probe.is_dangerous_command("mode_cool")


def test_dry_run_does_not_call_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run should work without credentials and never instantiate a client."""

    def fail_client(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("dry-run must not create a backend client")

    monkeypatch.setattr(probe, "BackendClient", fail_client)

    assert probe.main(["--dry-run", "--safe-suite"]) == 0
    output = capsys.readouterr().out

    assert '"dry_run": true' in output
    assert "DRY_RUN_DEVICE_ID" not in output
    assert "user_token" not in output


def test_execute_snapshot_respects_requested_device_id(tmp_path: Path) -> None:
    """Snapshot command should report the selected device, not just the first."""

    class FakeClient:
        def devices(self, installation_id: str) -> probe.HttpResult:
            assert installation_id == "installation-1"
            return probe.HttpResult(
                status=200,
                url="https://dkn.airzonecloud.com/devices",
                body=[
                    {"id": "device-1", "power": "0"},
                    {"id": "device-2", "power": "1"},
                ],
            )

    record, device = probe._execute_snapshot(
        FakeClient(),
        "installation-1",
        tmp_path,
        requested_device_id="device-2",
    )

    assert device == {"id": "device-2", "power": "1"}
    assert record["observed"]["id"] == "<REDACTED_ID>"
    assert record["observed"]["power"] == "1"


def test_sanitizer_writes_no_secrets(tmp_path: Path) -> None:
    """Synthetic historical artifacts should produce sanitized outputs only."""
    source = tmp_path / "evidence"
    source.mkdir()
    (source / "001.request.txt").write_text(
        "POST https://dkn.airzonecloud.com/events?"
        "user_email=owner@example.com&user_token=token-123\n"
        '{"event":{"cgi":"modmaquina","option":"P2","value":"1",'
        '"device_id":"device-1"}}',
        encoding="utf-8",
    )
    (source / "002.body.txt").write_text(
        json.dumps(
            [
                {
                    "id": "device-1",
                    "installation_id": "installation-1",
                    "name": "Bedroom",
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "power": "1",
                    "mode": "1",
                    "modes": "11111",
                    "cold_consign": "25.0",
                }
            ]
        ),
        encoding="utf-8",
    )

    output = tmp_path / "sanitized"
    result = sanitizer.sanitize_artifacts(source, output)
    combined_output = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    )

    assert result.files_processed == 2
    assert (output / "SUMMARY.sanitized.txt").exists()
    assert (output / "commands.jsonl").exists()
    assert (output / "snapshots.jsonl").exists()
    assert (output / "findings.md").exists()
    assert (output / "redaction_report.json").exists()
    assert "owner@example.com" not in combined_output
    assert "token-123" not in combined_output
    assert "device-1" not in combined_output
    assert "installation-1" not in combined_output
    assert "AA:BB:CC:DD:EE:FF" not in combined_output
    assert "Bedroom" not in combined_output
    assert '"cold_consign": "25.0"' in combined_output


def test_sanitizer_processes_safe_zip_entry(tmp_path: Path) -> None:
    """ZIP artifacts should be read in memory and sanitized."""
    archive_path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "001.request.txt",
            "GET https://dkn.airzonecloud.com/devices?"
            "user_email=owner@example.com&user_token=token-123\n"
            '{"event":{"cgi":"modmaquina","option":"P1","value":"1",'
            '"device_id":"device-1"}}',
        )

    output = tmp_path / "sanitized"
    result = sanitizer.sanitize_artifacts(archive_path, output)
    combined_output = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    )

    assert result.files_processed == 1
    assert "`/devices`" in (output / "findings.md").read_text(encoding="utf-8")
    assert "owner@example.com" not in combined_output
    assert "token-123" not in combined_output
    assert "device-1" not in combined_output


def test_sanitizer_ignores_unsafe_zip_entries(tmp_path: Path) -> None:
    """ZIP path traversal and absolute entries should be ignored."""
    archive_path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../evil.txt", "owner@example.com")
        archive.writestr("/absolute.txt", "token-123")
        archive.writestr(
            "safe/001.body.txt",
            json.dumps({"power": "1", "id": "device-1"}),
        )

    output = tmp_path / "sanitized"
    result = sanitizer.sanitize_artifacts(archive_path, output)
    combined_output = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    )

    assert result.files_processed == 1
    assert "owner@example.com" not in combined_output
    assert "token-123" not in combined_output
    assert "device-1" not in combined_output
    assert '"power": "1"' in combined_output
