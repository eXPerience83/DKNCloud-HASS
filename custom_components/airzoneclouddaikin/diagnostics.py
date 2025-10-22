"""Diagnostics for DKN Cloud for HASS (Airzone Cloud).

P1 hardening:
- Keep static redaction set (TO_REDACT).
- Add an extra regex-based redaction pass to catch any future key names
  that contain sensitive semantics (token/mail/email/mac/pin/lat/lon/gps/coord).
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Static fields to redact anywhere in the structure (nested keys included).
TO_REDACT = {
    # Auth & identity
    "email",
    "user_email",
    "password",
    "authentication_token",
    "token",
    "user_token",
    # Device identifiers / PII
    "mac",
    "pin",
    "installation_id",
    "spot_name",
    "complete_name",
    "time_zone",
    # Location data
    "latitude",
    "longitude",
    "lat",
    "lon",
    "location",
}

# Additional regex-based redaction for future/unknown keys
_RE_PATTERNS = [
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"mail|email", re.IGNORECASE),
    re.compile(r"\bmac\b", re.IGNORECASE),
    re.compile(r"\bpin\b", re.IGNORECASE),
    re.compile(r"lat|lon|gps|coord", re.IGNORECASE),
]


def _redact_by_regex(obj: Any) -> Any:
    """Recursively redact any dict key that matches our regex patterns."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if any(p.search(k) for p in _RE_PATTERNS):
                out[k] = "***"
            else:
                out[k] = _redact_by_regex(v)
        return out
    if isinstance(obj, list):
        return [_redact_by_regex(x) for x in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (sanitized)."""
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = domain_data.get("coordinator")

    entry_summary = {
        "title": entry.title,
        "data_keys": sorted(list(entry.data.keys())),  # keys only, no values
        "options": entry.options,
        "version": getattr(entry, "version", None),
    }

    coord_summary = None
    if coordinator is not None:
        interval = getattr(coordinator, "update_interval", None)
        if interval is not None:
            try:
                interval = interval.total_seconds()
            except Exception:  # noqa: BLE001
                interval = str(interval)
        coord_summary = {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "update_interval_seconds": interval,
            "devices_count": len(getattr(coordinator, "data", {}) or {}),
            "devices": getattr(coordinator, "data", {}),
        }

    raw = {"entry": entry_summary, "coordinator": coord_summary}

    # First, apply static redaction; then a defensive regex pass.
    redacted = async_redact_data(raw, TO_REDACT)
    return _redact_by_regex(redacted)
