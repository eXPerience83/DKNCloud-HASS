"""Sensors for DKN Cloud for HASS (Airzone Cloud).

0.4.0 metadata consistency:
- device_info now returns a DeviceInfo object (aligned with other platforms).
- Keep PII policy: sensors only created if expose_pii_identifiers=True; never log secrets.
- METADATA: Pass MAC via constructor `connections` using CONNECTION_NETWORK_MAC (no post-mutation).

Key points:
- Entities are created from coordinator.data (dict keyed by device_id).
- No I/O in properties; updates come via the DataUpdateCoordinator.
- Timestamps parsed with HA helpers (tz-aware).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .__init__ import AirzoneCoordinator
from .const import DOMAIN, MANUFACTURER
from .helpers import device_supports_heat_cool

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

# Non-diagnostic whitelist (daily-use sensors)
_NON_DIAG_WHITELIST = {
    "local_temp",
    "mode_text",
    "cold_consign",
    "heat_consign",
    "cold_speed",
    "heat_speed",
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
    ("modes", "Supported Modes (Bitmask)", "mdi:code-braces", True, None, None),
    ("status", "Status", "mdi:information-outline", True, None, None),
    ("mode", "Mode Code (Raw)", "mdi:code-braces", True, None, None),
    ("mode_text", "Mode (Text)", "mdi:format-list-bulleted", True, None, None),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True, None, None),
    ("firmware", "Firmware Version", "mdi:chip", True, None, None),
    ("brand", "Brand/Model", "mdi:factory", True, None, None),
    ("availables_speeds", "Available Fan Speeds", "mdi:fan", True, None, None),
    ("cold_consign", "Cool Setpoint", "mdi:snowflake", True, "temperature", None),
    ("heat_consign", "Heat Setpoint", "mdi:fire", True, "temperature", None),
    ("cold_speed", "Cool Fan Speed", "mdi:fan", True, None, None),
    ("heat_speed", "Heat Fan Speed", "mdi:fan", True, None, None),
]

# Diagnostics (some enabled by default per our decisions)
DIAG_SENSORS: list[tuple[str, str, str, bool, str | None, str | None]] = [
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check", True, None, None),
    ("power", "Power State (Raw)", "mdi:power", True, None, None),
    ("units", "Units", "mdi:ruler", False, None, None),
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
    (
        "ventilate_variant",
        "Ventilate Variant (3/8/none)",
        "mdi:shuffle-variant",
        True,
        None,
        None,
    ),
    (
        "heat_cool_supported",
        "HEAT_COOL Compatible",
        "mdi:autorenew",
        True,
        None,
        None,
    ),
    ("fan_modes_normalized", "Fan Modes Normalized", "mdi:fan", True, None, None),
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
    (
        "ver_state_slats",
        "Vertical Slats State",
        "mdi:unfold-more-vertical",
        False,
        None,
        None,
    ),
    (
        "ver_position_slats",
        "Vertical Slats Position",
        "mdi:unfold-more-vertical",
        False,
        None,
        None,
    ),
    (
        "hor_state_slats",
        "Horizontal Slats State",
        "mdi:unfold-more-horizontal",
        False,
        None,
        None,
    ),
    (
        "hor_position_slats",
        "Horizontal Slats Position",
        "mdi:unfold-more-horizontal",
        False,
        None,
        None,
    ),
    (
        "ver_cold_slats",
        "Vertical Slats (Cool Pattern)",
        "mdi:snowflake",
        False,
        None,
        None,
    ),
    ("ver_heat_slats", "Vertical Slats (Heat Pattern)", "mdi:fire", False, None, None),
    (
        "hor_cold_slats",
        "Horizontal Slats (Cool Pattern)",
        "mdi:snowflake",
        False,
        None,
        None,
    ),
    (
        "hor_heat_slats",
        "Horizontal Slats (Heat Pattern)",
        "mdi:fire",
        False,
        None,
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

    coordinator: AirzoneCoordinator | None = data.get("coordinator")
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

    # Cleanup PII entities when opted-out
    try:
        if not expose_pii:
            reg = er.async_get(hass)
            known_device_ids = set((coordinator.data or {}).keys())
            computed_pii_uids = {
                f"{dev_id}_{attr}" for dev_id in known_device_ids for attr in PII_ATTRS
            }
            for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
                if ent.domain != "sensor" or ent.platform != DOMAIN:
                    continue
                uid = (ent.unique_id or "").strip()
                if uid in computed_pii_uids:
                    reg.async_remove(ent.entity_id)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("PII cleanup skipped due to registry error: %s", exc)

    specs = list(CORE_SENSORS) + list(DIAG_SENSORS)
    if expose_pii:
        specs += PII_SENSORS

    entities: list[AirzoneSensor] = []
    for device_id in list((coordinator.data or {}).keys()):
        for spec in specs:
            entities.append(AirzoneSensor(coordinator, device_id, *spec))

    async_add_entities(entities)


class AirzoneSensor(CoordinatorEntity[AirzoneCoordinator], SensorEntity):
    """Read-only sensor surfacing fields from the device snapshot."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AirzoneCoordinator,
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
        self._is_pii: bool = attribute in PII_ATTRS

        self._attr_entity_category = (
            None
            if attribute in _NON_DIAG_WHITELIST or self._is_pii
            else EntityCategory.DIAGNOSTIC
        )
        self._attr_should_poll = False

        # Units
        if attribute in _TEMP_FLOAT_ATTRS:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif attribute == "sleep_time":
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
        else:
            self._attr_native_unit_of_measurement = None

        # Device & state classes
        if dev_class:
            try:
                self._attr_device_class = getattr(SensorDeviceClass, dev_class.upper())
            except Exception:  # noqa: BLE001
                self._attr_device_class = None
        if state_class:
            try:
                self._attr_state_class = getattr(SensorStateClass, state_class.upper())
            except Exception:  # noqa: BLE001
                self._attr_state_class = None

        self._attr_entity_registry_enabled_default = enabled_by_default

    @property
    def device_info(self) -> DeviceInfo:
        """Return unified Device Registry metadata.

        NOTE: We pass the MAC through the constructor 'connections' using
        CONNECTION_NETWORK_MAC and avoid mutating the object after creation.
        """
        dev = self._device
        mac = (str(dev.get("mac") or "").strip()) or None
        connections = {(CONNECTION_NETWORK_MAC, mac)} if mac else None

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
            model=dev.get("brand") or "Airzone DKN",
            sw_version=str(dev.get("firmware") or ""),
            name=dev.get("name") or "Airzone Device",
            connections=connections,
        )

    @property
    def _device(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._device_id, {})

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
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _parse_float6(val: Any) -> float | None:
        """Parse numeric to float with 6 decimals (for coordinates)."""
        if val is None:
            return None
        try:
            f = float(str(val).replace(",", "."))
            return round(f, 6)
        except Exception:  # noqa: BLE001
            return None

    @property
    def native_value(self) -> Any:
        # Temperatures / setpoints / limits / unoccupied -> float with 1 decimal
        if self._attribute in _TEMP_FLOAT_ATTRS:
            val = self._device.get(self._attribute)
            return self._parse_float1(val)

        # Timestamps -> tz-aware datetime (HA will display in local timezone)
        if self._attribute in _TIMESTAMP_ATTRS:
            val = self._device.get(self._attribute)
            try:
                ts = dt_util.parse_datetime(str(val)) if val else None
                return dt_util.as_local(ts) if ts is not None else None
            except Exception:  # noqa: BLE001
                return None

        # Friendly handling for machine_errors
        if self._attribute == "machine_errors":
            val = self._device.get(self._attribute)
            if val in (None, "", [], 0, "0"):
                return "No errors"
            if isinstance(val, list) or isinstance(val, tuple):
                return ", ".join(str(x) for x in val) if val else "No errors"
            return str(val)

        # Mode code as integer
        if self._attribute == "mode":
            val = self._device.get(self._attribute)
            try:
                return int(str(val))
            except Exception:  # noqa: BLE001
                return None

        # Mode text derived from `mode` code
        if self._attribute == "mode_text":
            code = str(self._device.get("mode", "")).strip()
            mapping = {
                "1": "cool",
                "2": "heat",
                "3": "ventilate",
                "4": "heat_cool",
                "5": "dry",
                "6": "cool_air",
                "7": "heat_air",
                "8": "ventilate (alt)",
            }
            return mapping.get(code, "unknown")

        # Ventilate variant from bitstring
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

        if self._attribute == "heat_cool_supported":
            return device_supports_heat_cool(self._device)

        # Whether fan modes are normalized (low/medium/high) or numeric (1..N)
        if self._attribute == "fan_modes_normalized":
            try:
                n = int(self._device.get("availables_speeds") or 0)
                return bool(n == 3)
            except Exception:  # noqa: BLE001
                return False

        # PII nested fields (latitude/longitude live under "location")
        if self._attribute in {"latitude", "longitude"}:
            loc = self._device.get("location") or {}
            raw = loc.get(self._attribute)
            return self._parse_float6(raw)

        # Plain values
        return self._device.get(self._attribute)
