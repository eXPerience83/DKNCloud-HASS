"""Diagnostics for DKN Cloud for HASS (Airzone Cloud).

Goal:
- Keep diagnostics useful for troubleshooting while protecting user privacy.
- Redact sensitive keys that appear in Airzone responses (devices + nested installation).
- Keep implementation minimal, aligned with the current style (key-based redaction).

Notes:
- We intentionally do not scrub string *values* (URLs, free text) to stay close to your
  current approach. If the backend ever injects secrets inside strings, consider adding
  a value-scrubbing pass in the future.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# --- Static redaction keys (redacted wherever they appear, nested included) ---
TO_REDACT = {
    # Auth & identity
    "email",
    "user_email",
    "password",
    "authentication_token",
    "token",
    "user_token",
    # Device/installation identifiers
    "mac",
    "pin",
    "serial",
    "uuid",
    "installation_id",
    "owner_id",
    "device_ids",
    # Installer / ownership contact
    "installer_email",
    "installer_phone",
    # Location / PII
    "location",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "postal_code",
    "spot_name",
    "complete_name",
    "time_zone",
}

# --- Defensive regex for *keys* (keep minimal, focused) ---
# Matches are case-insensitive and applied on dict keys only.
_RE_PATTERNS = [
    re.compile(r"token|auth(entication)?|secret|api.?key", re.IGNORECASE),
    re.compile(r"mail|email", re.IGNORECASE),
    re.compile(r"\bmac\b|\bpin\b|\buuid\b|\bserial\b", re.IGNORECASE),
    re.compile(r"lat|lon|gps|coord|location", re.IGNORECASE),
    re.compile(r"owner(_?id)?|installer|phone|postal|zip", re.IGNORECASE),
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
        "data_keys": sorted(list(entry.data.keys())),  # keys only, never values
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
            "heat_cool_single_setpoint": True,
            "heat_cool_routing": "cold(P7,P3)",
            "devices": getattr(coordinator, "data", {}),
        }

    raw = {"entry": entry_summary, "coordinator": coord_summary}

    # 1) Known keys
    redacted = async_redact_data(raw, TO_REDACT)
    # 2) Defensive regex pass (keys only; values unchanged)
    return _redact_by_regex(redacted)
