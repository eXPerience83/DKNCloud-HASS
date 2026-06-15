"""Tests for local backend artifact sanitizer helpers."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts import sanitize_backend_artifacts as sanitizer


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


def test_sanitizer_normalizes_backslash_zip_paths(tmp_path: Path) -> None:
    """Backslash ZIP paths should be normalized before safety checks."""
    archive_path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("safe\\001.body.txt", json.dumps({"power": "1"}))
        archive.writestr("..\\evil.txt", "owner@example.com")

    output = tmp_path / "sanitized"
    result = sanitizer.sanitize_artifacts(archive_path, output)
    snapshots = (output / "snapshots.jsonl").read_text(encoding="utf-8")
    combined_output = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    )

    assert result.files_processed == 1
    assert '"power": "1"' in snapshots
    assert "owner@example.com" not in combined_output


def test_sanitizer_processes_zip_inside_folder(tmp_path: Path) -> None:
    """Folder inputs should process ZIP files as archives, not binary text."""
    source = tmp_path / "evidence"
    source.mkdir()
    with zipfile.ZipFile(source / "nested.zip", "w") as archive:
        archive.writestr("001.body.txt", json.dumps({"mode": "2"}))

    output = tmp_path / "sanitized"
    result = sanitizer.sanitize_artifacts(source, output)

    assert result.files_processed == 1
    assert '"mode": "2"' in (output / "snapshots.jsonl").read_text(encoding="utf-8")


def test_sanitizer_missing_input_path_fails_clearly(tmp_path: Path) -> None:
    """Missing inputs should fail before producing output artifacts."""
    missing = tmp_path / "missing"
    output = tmp_path / "sanitized"

    with pytest.raises(FileNotFoundError):
        sanitizer.sanitize_artifacts(missing, output)

    assert not output.exists()
