"""Canonical cloud event payloads shared by DKN entity platforms.

Transport, retries, logging, and exception handling remain with the caller and API.
"""

from __future__ import annotations

from typing import Any


def modmaquina_payload(
    device_id: str, option: str, value: Any
) -> dict[str, dict[str, Any]]:
    """Build the exact DKN `POST /events` command body without sending it."""
    return {
        "event": {
            "cgi": "modmaquina",
            "device_id": device_id,
            "option": option,
            "value": value,
        }
    }
