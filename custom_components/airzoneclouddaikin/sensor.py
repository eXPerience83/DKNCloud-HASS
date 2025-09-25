"""Sensor platform for DKN Cloud for HASS (Airzone Cloud).

Consistent creation off coordinator.data (dict by device_id).
Core sensors enabled-by-default so they are visible out-of-the-box.
No I/O in properties; updates come from the coordinator.

Privacy: do not expose PIN as a sensor; redact secrets in logs.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ---------------------------
# Parsing/format groups
# ---------------------------
_TEMP_FLOAT_ATTRS = {
    "local_temp",
    "cold_consign",
    "heat_consign",
    "min_limit_cold",
    "max_limit_cold",
    "min_limit_heat",
    "max_limit_heat",
    "min_temp_unoccupied",
    "max_temp_unoccupied",
}
_TIMESTAMP_ATTRS = {"update_date", "connection_date"}

# PII attributes (must never be diagnostic and never logged)
PII_ATTRS = {
    "mac",
    "pin",
    "installation_id",
    "spot_name",
    "complete_name",
    "latitude",
    "longitude",
    "time_zone",
}

# ---------------------------
# Sensor specs (attribute, friendly, icon, enabled_by_default, device_class, state_class)
# ---------------------------
CORE_SENSORS: list[tuple[str, str, str, bool, str | None, str | None]] = [
    (
        "local_temp",
        "Local Temperature",
        "mdi:thermometer",
        True,
        "temperature",
        "measurement",
    ),
    ("sleep_time", "Sleep Timer (min)", "mdi:timer-sand", True, None, None),
    ("scenary", "Scenary", "mdi:account-clock", True, None, None),
    ("modes", "Supported Modes (Bitmask)", "mdi:toggle-switch", True, None, None),
    ("status", "Status", "mdi:information-outline", True, None, None),
    ("mode", "Mode Code (Raw)", "mdi:numeric", True, None, None),
    ("mode_text", "Mode (Text)", "mdi:format-list-bulleted", True, None, None),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True, None, None),
    ("firmware", "Firmware Version", "mdi:chip", True, None, None),
    ("brand", "Brand/Model", "mdi:factory", True, None, None),
    ("availables_speeds", "Available Fan Speeds", "mdi:fan", True, None, None),
    # Setpoints and fan speeds (kept enabled)
    ("cold_consign", "Cool Setpoint", "mdi:snowflake", True, "temperature", None),
    ("heat_consign", "Heat Setpoint", "mdi:fire", True, "temperature", None),
    ("cold_speed", "Cool Fan Speed", "mdi:fan", True, None, None),
    ("heat_speed", "Heat Fan Speed", "mdi:fan", True, None, None),
]

# Diagnostics (some enabled by default per our decisions)
DIAG_SENSORS: list[tuple[str, str, str, bool, str | None, str | None]] = [
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check", True, None, None),
    # IMPORTANT: 'power' requested enabled-by-default
    ("power", "Power State (Raw)", "mdi:power", True, None, None),
    # 'units' should be disabled-by-default
    ("units", "Units", "mdi:ruler", False, None, None),
    # Unoccupied ranges (enabled)
    (
        "min_temp_unoccupied",
        "Min Temp Unoccupied",
        "mdi:thermometer-low",
        True,
        "temperature",
        None,
    ),
    (
        "max_temp_unoccupied",
        "Max Temp Unoccupied",
        "mdi:thermometer-high",
        True,
        "temperature",
        None,
    ),
    # Device limits (enabled)
    (
        "min_limit_cold",
        "Min Limit Cool",
        "mdi:thermometer-chevron-down",
        True,
        "temperature",
        None,
    ),
    (
        "max_limit_cold",
        "Max Limit Cool",
        "mdi:thermometer-chevron-up",
        True,
        "temperature",
        None,
    ),
    (
        "min_limit_heat",
        "Min Limit Heat",
        "mdi:thermometer-chevron-down",
        True,
        "temperature",
        None,
    ),
    (
        "max_limit_heat",
        "Max Limit Heat",
        "mdi:thermometer-chevron-up",
        True,
        "temperature",
        None,
    ),
    # Ventilate variant (diagnostic, enabled): derive 3/8/none from modes bitmask
    (
        "ventilate_variant",
        "Ventilate Variant (3/8/none)",
        "mdi:shuffle-variant",
        True,
        None,
        None,
    ),
    # Timestamps (disabled by default as requested)
    (
        "update_date",
        "Last Update (Device)",
        "mdi:clock-check-outline",
        False,
        "timestamp",
        None,
    ),
    (
        "connection_date",
        "Last Connection",
        "mdi:clock-outline",
        False,
        "timestamp",
        None,
    ),
]

# PII sensors (created only when expose_pii_identifiers=True; not diagnostic)
PII_SENSORS: list[tuple[str, str, str, bool, str | None, str | None]] = [
    ("mac", "MAC Address", "mdi:lan", True, None, None),
    ("pin", "PIN", "mdi:key-variant", True, None, None),
    ("installation_id", "Installation ID", "mdi:identifier", True, None, None),
    ("spot_name", "Spot Name", "mdi:map-marker", True, None, None),
    ("complete_name", "Location (Full Name)", "mdi:home-map-marker", True, None, None),
    ("latitude", "Latitude", "mdi:map-marker-radius", True, None, None),
    ("longitude", "Longitude", "mdi:map-marker-radius-outline", True, None, None),
    ("time_zone", "Time Zone", "mdi:earth", True, None, None),
]


async def async_setup_entry(hass, entry, async_add_entities):
    """Create sensors according to coordinator snapshot and privacy options."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    # Read opt-in from options first, then data (setup step)
    opts = entry.options or {}
    expose_pii = bool(
        opts.get(
            "expose_pii_identifiers", entry.data.get("expose_pii_identifiers", False)
        )
    )

    # --- Cleanup of PII entities when opted-out (safe and narrow) ----------
    # We only remove previously-created PII sensors of THIS integration.
    # This does not touch non-PII sensors.
    try:
        if not expose_pii:
            reg = er.async_get(hass)
            for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
                if ent.domain != "sensor" or ent.platform != DOMAIN:
                    continue
                uid = (ent.unique_id or "").strip()
                # Remove ONLY if unique_id ends with one of the exact PII attribute names
                if any(uid.endswith(f"_{attr}") for attr in PII_ATTRS):
                    reg.async_remove(ent.entity_id)
    except Exception as exc:  # Defensive: never fail setup because of registry ops
        _LOGGER.debug("PII cleanup skipped due to registry error: %s", exc)

    specs = list(CORE_SENSORS) + list(DIAG_SENSORS)
    if expose_pii:
        specs += PII_SENSORS  # add PII when opted-in

    entities: list[AirzoneSensor] = []
    for device_id in list(coordinator.data.keys()):
        for spec in specs:
            entities.append(AirzoneSensor(coordinator, device_id, *spec))

    async_add_entities(entities)


class AirzoneSensor(CoordinatorEntity, SensorEntity):
    """Generic read-only sensor surfacing fields from device snapshot."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        device_id: str,
        attribute: str,
        friendly: str,
        icon: str,
        enabled_by_default: bool,
        dev_class: str | None,
        state_class: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attribute = attribute
        self._attr_name = friendly
        self._attr_icon = icon
        self._attr_unique_id = f"{device_id}_{attribute}"
        # Only "local_temp" and PII sensors are NOT diagnostic
        self._attr_entity_category = (
            None
            if attribute in {"local_temp", *PII_ATTRS}
            else EntityCategory.DIAGNOSTIC
        )
        self._attr_should_poll = False
        self._attr_native_unit_of_measurement = (
            UnitOfTemperature.CELSIUS if attribute in _TEMP_FLOAT_ATTRS else None
        )
        # Device & state classes
        if dev_class:
            try:
                self._attr_device_class = getattr(SensorDeviceClass, dev_class.upper())
            except Exception:
                self._attr_device_class = None
        if state_class:
            try:
                self._attr_state_class = getattr(SensorStateClass, state_class.upper())
            except Exception:
                self._attr_state_class = None
        # Default visibility
        self._attr_entity_registry_enabled_default = enabled_by_default

    @property
    def device_info(self):
        dev = self._device
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "manufacturer": "Daikin / Airzone",
            "model": dev.get("brand") or "Airzone DKN",
            "sw_version": dev.get("firmware") or "",
            "name": dev.get("name") or "Airzone Device",
        }

    @property
    def _device(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._device_id, {})

    @property
    def available(self) -> bool:
        return bool(self._device)

    @staticmethod
    def _parse_float1(val: Any) -> float | None:
        """Parse numeric string '23.0'/'23,0' and round to one decimal."""
        if val is None:
            return None
        try:
            f = float(str(val).replace(",", "."))
            return round(f, 1)
        except Exception:
            return None

    @staticmethod
    def _parse_float6(val: Any) -> float | None:
        """Parse numeric to float with 6 decimals (for coordinates)."""
        if val is None:
            return None
        try:
            f = float(str(val).replace(",", "."))
            return round(f, 6)
        except Exception:
            return None

    @property
    def native_value(self) -> Any:
        # Temperatures / setpoints / limits / unoccupied -> float with 1 decimal
        if self._attribute in _TEMP_FLOAT_ATTRS:
            val = self._device.get(self._attribute)
            return self._parse_float1(val)

        # Timestamps -> datetime (HA will display in local timezone)
        if self._attribute in _TIMESTAMP_ATTRS:
            val = self._device.get(self._attribute)
            try:
                return datetime.fromisoformat(str(val)) if val else None
            except Exception:
                return None

        # Friendly handling for machine_errors: collapse empty to "No errors"
        if self._attribute == "machine_errors":
            val = self._device.get(self._attribute)
            if val in (None, "", [], 0, "0"):
                return "No errors"
            if isinstance(val, list | tuple):
                return ", ".join(str(x) for x in val) if val else "No errors"
            return str(val)

        # Mode code exposed as integer
        if self._attribute == "mode":
            val = self._device.get(self._attribute)
            try:
                return int(str(val))
            except Exception:
                return None

        # Mode text derived from `mode` code.
        # Expanded to recognize P2=6/7/8 as per technical reference.
        if self._attribute == "mode_text":
            code = str(self._device.get("mode", "")).strip()
            mapping = {
                "1": "cool",
                "2": "heat",
                "3": "ventilate",  # shown as FAN_ONLY in HA climate
                "4": "auto (heat_cool)",
                "5": "dry",
                "6": "cool_air",
                "7": "heat_air",
                "8": "ventilate (alt)",  # alternate ventilate code
            }
            return mapping.get(code, "unknown")

        # Ventilate variant diagnostic: derive from 'modes' bitstring only.
        if self._attribute == "ventilate_variant":
            bitstr = str(self._device.get("modes") or "")
            if bitstr and all(ch in "01" for ch in bitstr):
                sup3 = len(bitstr) >= 3 and bitstr[2] == "1"
                sup8 = len(bitstr) >= 8 and bitstr[7] == "1"
                if sup3:
                    return "3"
                if sup8:
                    return "8"
            return "none"

        # PII nested fields (latitude/longitude live under "location")
        if self._attribute in {"latitude", "longitude"}:
            loc = self._device.get("location") or {}
            raw = loc.get(self._attribute)
            return self._parse_float6(raw)

        # Plain values (status, brand, firmware, etc.)
        return self._device.get(self._attribute)
