"""Shared state interpretations for DKN entities.

Unknown backend power values are deliberately not treated as proof of OFF
by the coordinator's sleep-expiry logic. Power readback likewise requires an
explicit valid value to resolve an uncertain write.
"""

from __future__ import annotations

from typing import Any

from .const import SCENARY_HOME, SCENARY_SLEEP, SCENARY_VACANT

_SCENARY_TO_PRESET = {
    SCENARY_HOME: "home",
    SCENARY_VACANT: "away",
    SCENARY_SLEEP: "sleep",
}
_PRESET_TO_SCENARY = {preset: scenary for scenary, preset in _SCENARY_TO_PRESET.items()}


def power_is_on(value: Any) -> bool:
    """Normalize optimistic/backend ON values with the existing entity semantics."""
    text = str(value).strip().lower()
    if text in ("1", "on", "true", "yes"):
        return True
    if text in ("0", "off", "false", "no", "", "none"):
        return False
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except Exception:  # noqa: BLE001
        return False


def confirm_power_read(
    uncertain: dict[str, object],
    at_dispatch: dict[str, object],
    data: dict[str, dict[str, Any]],
    fresh_power_ids: set[str],
) -> None:
    """Resolve only uncertainty that existed when a GET began."""
    for device_id in fresh_power_ids & at_dispatch.keys():
        if uncertain.get(device_id) is at_dispatch[device_id] and str(
            data[device_id].get("power")
        ).strip().lower() in {"0", "1", "false", "true", "off", "on"}:
            del uncertain[device_id]


def scenary_to_preset(scenary: str | None) -> str | None:
    """Return a supported HA preset, or None for unknown backend values."""
    return _SCENARY_TO_PRESET.get((scenary or "").strip().lower())


def preset_to_scenary(preset: str) -> str | None:
    """Return a supported backend scenary, or None for unknown HA presets."""
    return _PRESET_TO_SCENARY.get((preset or "").strip().lower())
