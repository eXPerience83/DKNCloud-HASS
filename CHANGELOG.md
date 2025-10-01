# Changelog

## [0.3.8-alpha.02] - 2025-10-01
### Changed
- HTTP timeout raised to **30s** (from 15s) to better align with HA defaults and slow links.
- Coordinator is now a small **typed subclass**, exposing `api: AirzoneAPI` without ad-hoc attributes.
### Fixed
- Climate: **idempotent** `async_set_hvac_mode` — if the requested mode is already active and power is ON, skip sending redundant `P2`.
### Docs
- README/info: updated networking section to reflect the 30s timeout.
- Pre-release version formatted as `0.3.8a2` to ensure proper ordering in HACS.

## [0.3.8-alpha.1] - Unreleased
### Changed
- HTTP: Centralized browser-like User-Agent and endpoint-specific minimal headers.
  - GET `/devices`: only `User-Agent` (matches cURL usage).
  - POST `/events`: `User-Agent`, `X-Requested-With`, `Content-Type`, `Accept`.
- Internals: Default request headers are now minimal; endpoint-specific headers are injected where required.

## [0.3.7] - 2025-09-28
### Fixed
- switch: ensure stable device identifier even when coordinator snapshot is empty at startup (fallback to `self._device_id`).
- climate: do not swallow API errors; `_send_p_event()` now re-raises (except `CancelledError`). Callers only apply optimistic state and schedule refresh after a successful send, preventing “phantom success” in the UI.
- - Moved `import time` to module level in `select.py` and `number.py` to avoid per-read imports in entity properties and improve async hygiene.
### Notes
- `P2=4 (AUTO)` remains unsupported for now; docs toggle will be added when implemented.

## [0.3.7-alpha.11] - 2025-09-27
### Changed
- Presets UI: `scenary` now appears under **Controls** (entity_category=None) and `sleep_time` now appears under **Configuration** (entity_category=CONFIG) for clearer organization.
- No other logic changes: unique IDs, optimistic updates, and coordinator refresh behavior remain the same.

## [0.3.7-alpha.9] - 2025-09-27
### Added
- Binary Sensor: new `device_on` (device_class: power), enabled by default and non-diagnostic. Mirrors the backend `power` field with robust normalization for dashboards/automations. No I/O in properties; reads from the coordinator snapshot.
### Changed
- Core: `__init__.py` now always loads the `binary_sensor` platform (minimal change—no other behavior altered).
### Notes
- Presets (`select`/`number`) remain always loaded as of 0.3.7-alpha.8.
- No translation updates in this build.

## [0.3.7-alpha.8] - 2025-09-27
### Changed
- Presets are now **always loaded** from `__init__.py` (`select.py` and `number.py` are forwarded unconditionally). The previous `enable_presets` toggle is ignored by setup.
- Options: removed **`enable_presets`** from the config & options flow. Presets (select/number) are now always loaded (per previous step), so the flag is no longer needed.
- Options UI now only exposes **`scan_interval`** (min 10s) and **`expose_pii_identifiers`**.
- Sensors: extended the **non-diagnostic whitelist** to `{local_temp, mode_text, cold_consign, heat_consign, cold_speed, heat_speed}` so these show under regular *Sensors* instead of *Diagnostics*.
### Added
- Sensors: added **slats** telemetry as **diagnostic disabled-by-default** (`ver_state_slats`, `ver_position_slats`, `hor_state_slats`, `hor_position_slats`, `ver_cold_slats`, `ver_heat_slats`, `hor_cold_slats`, `hor_heat_slats`).
### Unchanged (confirmed)
- Defaults preserved: `power (raw)` remains diagnostic **enabled**; `units`, `update_date`, `connection_date` remain diagnostic **disabled**.
- PII cleanup remains **narrow** (removes only sensors whose unique_id ends with a PII attribute) to avoid deleting non-PII entities.

## [0.3.7-alpha.7] - 2025-09-25
### Fixed
- Options: restored `enable_presets` (select/number) by reading from `options` **and** falling back to `data` during setup.
- Sensors: `power` is now enabled by default; `units`, `update_date` and `connection_date` remain disabled by default (documented).
- Privacy: hardened PII cleanup when `expose_pii_identifiers` is disabled, ensuring non-PII sensors are unaffected.
### Notes
- Home Assistant does not retroactively disable already-created entities when defaults change. If `units/update_date/connection_date` appear enabled from previous versions, disable them from the UI or remove the entities so they are re-created with the new defaults.

## [0.3.7-alpha.6] - 2025-09-25
### Added
- Privacy: automatic cleanup of PII sensors when `expose_pii_identifiers` is disabled (entities are removed from the Entity Registry on entry reload).

## [0.3.7-alpha.5] - 2025-09-25
### Added
- Sensors: new `status`, `mode` (raw), and derived `mode_text` (maps 1→cool, 2→heat, 3→fan_only, 4→auto/heat_cool, 5→dry; unknown otherwise), all enabled by default.
- Sensors: `min_limit_cold/max_limit_cold/min_limit_heat/max_limit_heat` and `min/max_temp_unoccupied` are now enabled by default; all temperature-like sensors display **1 decimal** for consistency.
- Sensors: `update_date` and `connection_date` added as `timestamp` (disabled by default).
- - Privacy/PII: sensors for `mac`, `pin`, `installation_id`, `spot_name`, `complete_name`, `latitude`, `longitude`, and `time_zone` are created **only when** the new `expose_pii_identifiers` option is enabled; when enabled they are **on by default** and are **not** marked as diagnostic.
- Sensor: `ventilate_variant` (diagnostic, enabled by default) derived from the `modes` bitmask; values: `"3"` (preferred), `"8"` (fallback), or `"none"`.
### Changed
- Sensors: `progs_enabled` is enabled by default.
- Formatting: all temperature/setpoint/limit/unoccupied values are parsed safely and rounded to **one decimal**.
- Sensor: `mode_text` mapping extended to include P2=6 (`cool_air`), P2=7 (`heat_air`), and P2=8 (`ventilate (alt)`), keeping existing mappings for 1/2/3/4/5.
### Security/Privacy
- Config/Options: added `expose_pii_identifiers` opt-in flag (stored only; PII is never logged or included in diagnostics).
### Notes
- Disabling `expose_pii_identifiers` stops providing PII sensors on next reload; any previously created PII entities remain in Home Assistant's entity registry (standard behavior) and can be removed manually from the UI if desired.

## [0.3.7-alpha.4] - 2025-09-23
### Added
- Options Flow for editing **scan_interval** and **enable_presets** from the UI.
### Fixed
- Missing “Options” menu in the integration due to `async_get_options_flow` not being defined on the `ConfigFlow` class.
- Ensured `config_flow.py` is complete and Black/Ruff compliant (no truncated lines).

## [0.3.7-alpha.3] - 2025-09-22
### Fixed
- Options Flow now shows correctly: `async_get_options_flow` moved to module level in `config_flow.py`.
- Robust installation parsing in `__init__.py` (handles both `installation.id` and `installation_id`).
- `airzone_api.login()` now accepts tokens at either `resp.user.authentication_token` or `resp.authentication_token`.
### Added
- Options editable post-setup: `scan_interval` and `enable_presets`.
- Conditional loading of `select` (scenary) and `number` (sleep_time) platforms when `enable_presets` is enabled.
- Optimistic UI for scenary and sleep_time entities.
### Changed
- Defensive checks to avoid loading optional platforms when files are missing.

## [0.3.7-alpha.2] - 2025-09-22
### Fixed
- Options Flow registration: moved `async_get_options_flow()` to the `ConfigFlow` class so the **Options** button appears and the flow is callable by Home Assistant.
- Runtime settings now read from `entry.options` (with fallback to `entry.data`), so `scan_interval` and `enable_presets` actually take effect after editing options.
### Changed
- Added an options update listener to **reload the entry** when options change.
- Conditional platform loading: `select` and `number` are now loaded only when `enable_presets` is enabled.

## [0.3.7-alpha.1] - 2025-09-22
### Added
- Options flow: `enable_presets` flag and editable `scan_interval` (min 10s).
- `airzone_api.put_device_fields()`: generic PUT `/devices/<id>` helper with retries/backoff and PII-safe logging.
- `airzone_api.put_device_scenary()` and `put_device_sleep_time()` built on the generic PUT.
- `select.py`: Scenary control (`occupied` / `vacant` / `sleep`) using the API helper, with optimistic UI.
- `number.py`: Sleep timer control (`sleep_time` 30..120, step 10) using the API helper, with optimistic UI.
### Changed
- Presets (select/number) are optional: entities are enabled-by-default only if `enable_presets` is set; otherwise they are created disabled so users can enable them manually if desired.
### Fixed
- Ensured no PII in logs for all new PUT flows, consistent with existing API client behavior.

## [0.3.5-alpha.5] - 2025-09-18
### Added
- Climate: implement `async_turn_on`/`async_turn_off` mapped to `P1` with optimistic state and short post-write refresh.
### Fixed
- Climate: normalize optimistic/remote power values in `_device_power_on()` so `"0"` is correctly treated as `False`, enabling reliable auto-power-on when changing HVAC mode.

## [0.3.5-alpha.4] - 2025-09-17
### Changed
- Climate: enforce integer UI step for target temperature by adding `target_temperature_step = 1.0` while keeping `precision = PRECISION_WHOLE`. This guarantees 1°C increments in UI to match device capabilities.

## [0.3.5-alpha.3] - 2025-09-16
### Fixed
- Sensors: `machine_errors` now reports **"No errors"** when the backend returns a null/empty value, instead of showing `unknown`. If a list of errors is returned, it is rendered as a comma-separated string; other values are shown as-is. No other sensor behavior changed.
- Prevent crash on entity setup caused by `climate.supported_features` returning a plain `int`. Now it always returns a proper `ClimateEntityFeature` bitmask, avoiding `TypeError: argument of type 'int' is not iterable` on recent HA versions.
- Sensors: `local_temp` (and also `cold_consign` / `heat_consign`) could show as `unknown` when the backend returned decimal values (e.g. "23.5"). Parsing now uses `float(...)` instead of `int(...)`, restoring proper readings without altering entity names or structure.
- Climate: restore correct `/events` payload using `{"cgi":"modmaquina","device_id":..., "option":"P#", "value":...}`. Commands (power/mode/temp/fan) now operate reliably, mirroring the switch entity's format.
- Climate: parse `target_temperature`, `min_temp`, and `max_temp` as floats (accepting `"24.0"` or `"23,5"`). This prevents `unknown` setpoint values and avoids falling back to default limits when the backend returns decimal strings. Clamping for P7/P8 remains integer-based, and payload still sends `"NN.0"`.
### Changed
- Rebalanced default sensors: core ones enabled by default again (e.g., `local_temp`, scenary, speeds, consigns); extra diagnostics remain opt-in to reduce noise and protect privacy.
- Climate: interpret `device["modes"]` as a *bitstring* (positions P2=1..8) instead of an integer bitmask; fallback to exposing COOL/HEAT/FAN_ONLY/DRY when missing.

### [0.3.5-alpha.2] - 2025-09-16
#### Added
- Diagnostic sensors for **MAC Address** and **PIN** (disabled by default).
- Timestamp sensors for **Connection Date** and **Device Update Date** (disabled by default).
- **Sleep Timer (min)** is now enabled by default.
#### Changed
- Normalize value types:
  - Temperatures and sleep minutes shown as **integers** in the UI.
  - Proper duration unit for sleep timer (minutes) with `device_class: duration`.
- Climate:
  - Restored **fan control**; hidden in **Dry** and **Off**.
  - Respect device **modes** bitmask when provided.
  - Target temperature uses 1 °C steps in UI; API payload remains decimal.
  - **Device Registry**: climate entity now also reports **model** (brand) and **sw_version** (firmware), aligned with the power switch.
#### Fixed
- Climate init crash when accessing device snapshot before context was built.
- Write commands use the correct **/events** payload (P1/P2/P3/P4/P7/P8).

## [0.3.5-alpha.1] - 2025-09-15
### Fixed
- API: Catch builtin `TimeoutError` (Python 3.11 alias of `asyncio.TimeoutError`) to align with Ruff/pyupgrade and avoid formatter rewrites.
- API: Correct minor header typo when setting `Content-Type`.
- Diagnostics: Prevent duplicated titles in diagnostic sensors when the backend device name changes; names are rebuilt as `<DeviceName> <FriendlyName>` on each update.
- Hassfest: Ensure manifest keys are strictly sorted (domain, name, then alphabetical).
- HACS: `hacs.json` uses a supported minimal schema (`name`, `render_readme`).
### Changed
- Manifest version bumped to `0.3.5-alpha.1` for the first alpha of this phase.
### Notes
- If HACS complains about pre-release formatting, use `0.3.5a1` as an alternative version string in the manifest (PEP 440 compatible).

## [0.3.4] - 2025-06-28
### **Changed**
- Removed HEAT_COOL (dual setpoint/auto) mode from all logic, mapping, and documentation after confirming with real-world API tests that this Daikin/Airzone unit does not support it.
- Now only COOL, HEAT, FAN_ONLY, and DRY are available HVAC modes.
- Documentation and info.md updated with technical details about these findings.

## [0.3.3] - 2025-06-27
### **Added**
- Added `device_class = temperature`, `unit_of_measurement = °C`, and `state_class = measurement` to all sensors representing temperature values:
  - `cold_consign`, `heat_consign`
  - `min_temp_unoccupied`, `max_temp_unoccupied`
  - `min_limit_cold`, `max_limit_cold`
  - `min_limit_heat`, `max_limit_heat`
### **Changed**
- Applied `state_class = measurement` to fan speed and available speed sensors:
  - `cold_speed`, `heat_speed`, `availables_speeds`
- Improved visibility and UI representation of temperature-related sensors with proper device classes.
- Refactored internal handling of `machine_errors` to display its value as-is (including future support for string or list), showing "No errors" when empty or null.

## [0.3.2] - 2025-06-27
### **Added**
- Diagnostic sensors for:  
  - `power` (raw on/off value)
  - `units` (system units)
  - `availables_speeds` (number of available fan speeds)
  - `local_temp` (device temperature, raw)
  - `cold_consign` (cooling setpoint, raw)
  - `heat_consign` (heating setpoint, raw)
  - `cold_speed` (current cool fan speed)
  - `heat_speed` (current heat fan speed)
### **Changed**
- Diagnostic sensors for less commonly used fields (e.g., slats, some raw and advanced diagnostics) are now **disabled by default**—can be enabled via the Home Assistant UI.
- Improved naming and icons for new and existing diagnostic sensors.
- All changes maintain full backwards compatibility for existing users.
### **Removed**
- Removed location and place fields from diagnostic sensors:  
  - `time_zone`, `spot_name`, `complete_name` are no longer exposed as entities.

## [0.3.1] - 2025-06-27
### **Added**
- Exposed **all available diagnostic fields** from the Airzone API response as individual sensors with `EntityCategory.DIAGNOSTIC`.  
  This includes: machine_errors, firmware, brand, pin, update_date, mode (raw), all vertical/horizontal slats states/positions (for both current and hot/cold), in addition to previously available diagnostics (scenary, program enabled, sleep time, etc).
- Each sensor uses a unique icon and human-friendly name for clarity in the Home Assistant UI.
### **Changed**
- `sensor.py` refactored for clarity, extensibility, and clean inline documentation.
- Now easier to add/remove diagnostic sensors by editing a single array.


## [0.3.0] - 2025-06-27
### **Changed**
- **Professional README.md rewrite:**  
  - Reworked all documentation in the style of modern Home Assistant custom integrations, with shields/badges, clear features, compatibility table, roadmap, FAQs, security notice, and explicit Acknowledgments to [max13fr/AirzoneCloudDaikin](https://github.com/max13fr/AirzoneCloudDaikin).
  - Added multi-language and roadmap notes, and improved explanation of dual setpoint (HEAT_COOL) mode.
  - Improved documentation for slat position fields, diagnostics, and control API mapping.
  - Security warning added about never sharing real tokens, emails, or IDs.
### **Added**
- **Diagnostic sensors:**  
  Exposes additional diagnostic entities for modes, scene/presets, program status, sleep timer, slats positions, and temperature ranges (including fields like `ver_state_slats`, `ver_position_slats`, `hor_state_slats`, `hor_position_slats`, `ver_cold_slats`, `ver_heat_slats`, `hor_cold_slats`, `hor_heat_slats`).
- **Dual setpoint support:**  
  Climate entity now supports both `target_temperature_high` and `target_temperature_low` in HEAT_COOL mode, sending both P7 (cool) and P8 (heat) as needed.
- **Roadmap section:**  
  Added initial roadmap in README for multi-language and more diagnostics.
- **Funding links:**  
  Added Ko-fi and PayPal as main donation channels; enabled `.github/FUNDING.yml` for repository.
### **Fixed**
- **API/Device field documentation:**  
  All fields in `info.md` examples are now fully anonymized, including slats and installation/location data.
### **Removed**
- **Legacy or generic mentions:**  
  Updated documentation to clarify this fork is now a stand-alone, modern Home Assistant integration and not a simple derivative.

## [0.2.8] - 2025-04-09
### **Fixed**  
- **Compatibility with Home Assistant 2025.4+:**  
  - Resolved critical errors caused by `TypeError: argument of type 'int' is not iterable` in `climate.py` when evaluating supported features.
  - Removed unsupported checks like `if ClimateEntityFeature.X in supported_features`, replacing them with bitwise operations to avoid compatibility issues with bitmask flags.
  - Eliminated invalid imports such as `ATTR_MIN_TEMP` and `ATTR_MAX_TEMP` from `homeassistant.const` (now imported from `homeassistant.components.climate`).
### **Changed**  
- **Climate Entity (`climate.py`):**
  - Now hides fan controls (fan mode and fan speeds) when HVAC mode is `OFF` or `DRY`.
  - Updated `fan_modes` and `fan_mode` properties to return empty list or `None` when fan control is not applicable.
  - The `supported_features` property dynamically adjusts the capabilities based on the current HVAC mode, removing temperature and fan control when in unsupported modes (`OFF`, `DRY`, or `FAN_ONLY`).
  - Improved `capability_attributes` to conditionally expose supported capabilities, avoiding crashes in Home Assistant core when rendering UI elements.
  - Removed unnecessary overrides and obsolete code that caused entity registration failures (`NoEntitySpecifiedError` and `AttributeError: __attr_hvac_mode`).
### **Improved**  
- **Codebase Maintenance:**
  - Refactored legacy entity attribute assignments to fully comply with Home Assistant’s internal attribute naming conventions.
  - Ensured all inline comments are in English for consistency across the repository.
  - Eliminated the use of `async_write_ha_state()` or thread-safe update calls that conflicted with Home Assistant's async context in 2025.4 and beyond.
  - Improved fault tolerance in `set_temperature()` and `set_fan_speed()` by preventing unsupported operations in certain modes with clear warnings in the logs.
### **Notes**
- This version ensures full compatibility with Home Assistant 2025.4+, especially regarding the updated behavior of `ClimateEntityFeature` and entity lifecycle handling.
- The fan control now gracefully disappears in modes where it’s not applicable (`DRY`, `OFF`), improving UI clarity and preventing invalid operations.
- Restart Home Assistant after updating to ensure all entity states and services are reloaded correctly.

## [0.2.7] - 2025-03-26  
### **Fixed**  
- **Switch Entity (`switch.py`)**:  
  - Resolved an issue where the switch entity (`AirzonePowerSwitch`) was not updating its state properly when the device was manually turned on/off.  
  - Implemented `schedule_update_ha_state()` in `async_turn_on()`, `async_turn_off()`, and `async_update()` to ensure real-time state updates in Home Assistant.  
  - Ensured that polling the device state correctly updates the power switch status.
- **Climate Entity (`climate.py`)**:  
  - Fixed an issue where setting a new HVAC mode while the device was off did not turn it on automatically.  
  - Now, if the HVAC mode is OFF and a new mode is set, the system first turns on the device before applying the selected mode.
### **Changed**  
- **Code Refactoring & Consistency**:  
  - Renamed `HVAC_MODE_AUTO` to `HVACMode.AUTO` for consistency across all files.  
  - Removed unused `set_preset_mode()` function from `climate.py` since preset modes are not yet implemented.  
  - Ensured all comments and logs are in English for consistency and readability.  
  - Improved error handling and logging for better debugging and traceability.
### **Improved**  
- **Temperature Sensor (`sensor.py`)**:  
  - Added predefined display precision to show temperature as whole numbers instead of decimals (e.g., `22°C` instead of `22.0°C`).  
  - This improves visual consistency as the Airzone API only returns integer values for temperature.
### **Notes**  
- This update enhances synchronization between Home Assistant and the Airzone Cloud Daikin API, ensuring real-time updates of device states.
- Restart Home Assistant after updating to apply all fixes.

## [0.2.6] - 2025-03-23  
### **Fixed**  
- **Switch Entity (`switch.py`)**:  
  - Resolved an issue where the power switch entity (`AirzonePowerSwitch`) was not updating its state when the device was turned on/off manually or from other entities.  
  - Implemented periodic polling of the device status to fetch the latest `power` state.  
  - Ensured `unique_id` is always assigned before adding the entity to Home Assistant.  
  - Added a safeguard to prevent `async_write_ha_state()` from being called when `unique_id` is missing, preventing `NoEntitySpecifiedError`.  
  - Improved logging to help debug state updates and API response handling.  
### **Changed**  
- Enhanced `async_update()` in `switch.py` to check for missing `installation_id` before fetching data.  
### **Notes**  
- This update improves power state synchronization between Home Assistant and the Airzone Cloud Daikin API.  
- Restart Home Assistant after updating to apply these fixes.  

## [0.2.5] - 2025-03-20
### Added
- Updated version to 0.2.5.
- Corrected device information in the device registry:
  - The “firmware” value now correctly uses the firmware field (e.g. "1.0.1") instead of the update_date.
  - The “model” field is set to the value of "brand" (e.g. "ADEQ125B2VEB") and is labeled as “Model”.
- Extended info.md to include details about the “modes” field (binary mapping for P2) and how it correlates with standard HVAC modes:
  - Mapping: P2 "1" → COOL, "2" → HEAT, "3" → FAN ONLY, "4" → AUTO, "5" → DRY; states 6–8 are considered Unavailable.
- Updated device_info in climate.py to remove unsupported keywords and correctly display device attributes.
- Minor improvements to logging and error handling.
### Changed
- Revised handling of device data updates in climate.py.
- Adjusted fan speed commands: now using P3 for COOL/FAN_ONLY modes and P4 for HEAT/AUTO modes.
- Removed any references to a forced scan interval; the integration now relies on Home Assistant’s built-in polling mechanism.
- Cleaned up comments and updated documentation to reflect all changes in English.
- Fixed issues with the sensor entity unique ID to avoid “No entity id specified” errors.
### Pending
- Further testing on additional machine models.
- Investigate potential support for additional commands (e.g., preset modes) and swing mode adjustments.
- Add the “pin” value to the device info alongside the MAC address.

## [0.2.4] - 2025-03-19
### Changed
- Climate Platform (climate.py):
  - Fixed the error “no running event loop” by replacing asyncio.create_task with self.hass.async_create_task in _send_command.
  - Ensured unique_id is set using the device id.
- Sensor Platform (sensor.py):
  - Added async_setup_entry so that sensor entities are loaded from the config entry.
- Renamed "heat-cold-auto" to HVACMode.AUTO (module-level constant HVAC_MODE_AUTO) in the code.

## [0.2.3] - 2025-03-19
### Added
- Updated version to 0.2.3.
- Fixed unique_id for both climate and sensor entities so they are properly registered and managed in the Home Assistant UI.
- Added asynchronous method `send_event` to the AirzoneAPI class in airzone_api.py.
- Updated config_flow.py to include the "force_hvac_mode_auto" option.
- Updated set_temperature in climate.py to constrain values based on device limits (min_limit_cold/max_limit_cold for cool modes; min_limit_heat/max_limit_heat for heat modes) and format the value as an integer with ".0".
- Updated info.md with the original MODES_CONVERTER mapping from max13fr and detailed curl command examples (using generic placeholders).
### Changed
- Replaced deprecated async_forward_entry_setup with async_forward_entry_setups in __init__.py.
- Updated imports in climate.py and sensor.py to use HVACMode, ClimateEntityFeature, and UnitOfTemperature.
- Renamed "heat-cold-auto" to HVACMode.AUTO in the code.
- Updated README.md with full integration details in English.
- Updated all text and comments to English.
### Pending
- Further verification of fan speed control in different modes.
- Additional testing of HVACMode.AUTO behavior on various machine models.

## [0.2.2] - 2025-03-19
### Added
- Updated version to 0.2.2.
- In the API calls for installations, now includes "user_email" and "user_token" in query parameters.
- Added support for controlling fan speed:
  - P3 for fan speed in cool (ventilate) mode.
  - P4 for fan speed in heat/auto mode.
- Renamed "heat-cold-auto" to HVACMode.AUTO in all code.
- In set_temperature, the temperature is now constrained to the limits (min_limit_cold/max_limit_cold or min_limit_heat/max_limit_heat) from the API; the value is sent as an integer with ".0" appended.
- Added the configuration option "force_hvac_mode_auto" (in config_flow) to enable the forced auto mode.
- Updated info.md with the original MODES_CONVERTER mapping from max13fr, with a note that only modes 1–5 produced effect in our tests (model ADEQ125B2VEB).
### Changed
- Minor adjustments in config_flow.py, climate.py, and README.md.
- Pending: Verify additional fan speed adjustments for FAN_ONLY and HVACMode.AUTO modes.

## [0.2.1] - 2025-03-19
### Added
- Integration updated to version 0.2.1.
- Added configuration option "force_heat_cold_auto" in config_flow (allows forcing the "heat-cold-auto" mode).
- Updated async_setup_entry in climate.py to pass configuration to each climate entity.
- In AirzoneClimate (climate.py), the property hvac_modes now includes "heat-cold-auto" if force_heat_cold_auto is enabled.
- Added property fan_speed_range in climate.py to dynamically generate the list of valid fan speeds from the API field "availables_speeds".
- Added sensor platform (sensor.py) for recording the temperature probe (local_temp).
- Updated info.md with detailed documentation on the original MODES_CONVERTER mapping from max13fr and noted that in our tests, only modes 1–5 produce an effect (for model ADEQ125B2VEB).
- Updated README.md with clarifications on available features, including differences in fan speed settings for cool and heat modes.
### Changed
- Version updated in manifest.json to 0.2.1.
- Minor adjustments in config_flow.py, __init__.py, and README.md.
- Pending: Verify fan speed adjustment in FAN_ONLY and HVACMode.AUTO modes.

## [0.2.0] - 2025-03-19
### Added
- Integration updated to version 0.2.0.
- Updated API endpoints and BASE_URL in const.py.
- Simplified const.py (removed MODES_CONVERTER from code; it is documented in info.md).
- Updated User-Agent in airzone_api.py to simulate a Windows browser.
- Implemented basic control methods in climate.py:
  - turn_on, turn_off, set_hvac_mode (including support for "heat-cold-auto" via P2=4, forced under user responsibility),
  - set_temperature (using P8 for heat and P7 for cool, with temperature values sent as decimals).
- Added property `fan_speed_range` in climate.py to derive allowed fan speeds from "availables_speeds".
- Added sensor platform (sensor.py) for a temperature probe sensor to record the "local_temp".
- Added file info.md with detailed information about the "Px" modes and example curl commands (using placeholders for sensitive data).
- Documented that the original package defined modes up to "8", but in our tests only modes 1–5 produce an effect.
### Changed
- Version updated in manifest.json to 0.2.0.
- Minor adjustments in config_flow.py, __init__.py, and README.md.
### Pending (for future versions)
- Refinement of control actions in climate.py if needed.
- Further testing and potential implementation of additional options (P5, P6) if required.

## [0.1.5] - 2025-03-16
### Added
- **Airzone API Client:** Updated module airzone_api.py now uses the endpoints from the original AirzoneCloudDaikin package:
  - Login: `/users/sign_in`
  - Installation Relations: `/installation_relations`
  - Devices: `/devices`
  - Events: `/events`
- Added a method `fetch_devices(installation_id)` in airzone_api.py to retrieve devices for a given installation.
- Updated climate.py to fetch devices per installation using `fetch_devices`.
- Added detailed logging to all modules for debugging (login, fetching installations, fetching devices).
### Fixed
- Updated endpoints based on tests with curl.
- Adjusted the base URL to "https://dkn.airzonecloud.com" (without adding "/api") as used in the original package.
- Fixed import errors by ensuring const.py exports the required constants.
- Set version in manifest.json to 0.1.5.
### Changed
- Documentation in README.md updated to reflect these changes.

## [0.1.2] - 2025-03-16
### Added
- **Airzone API Client:** New module airzone_api.py implementing the official Airzone Cloud Web API (adapted for dkn.airzonecloud.com) for authentication and fetching installations.
- **Async Setup:** Updated climate.py now uses async_setup_entry to initialize the integration with config entries.
- Added detailed logging in airzone_api.py and climate.py for better debugging of API calls and data retrieval.
- Added a new const.py with essential constants.
### Fixed
- Replaced external dependency on the AirzoneCloudDaikin package by using our own implementation.
- Updated the version in manifest.json to 0.1.2.
- Fixed import errors by creating/updating const.py.
### Changed
- Updated documentation in README.md to reflect changes.
- Updated repository URLs and HACS configuration.

## [0.1.1] - 2025-03-15
### Fixed
- Replaced setup_platform with async_setup_entry in climate.py to support Home Assistant's config entries.
- Fixed integration with AirzoneCloudDaikin library in climate.py.
- Updated entity setup to use async_add_entities instead of add_entities.
- Optimized HVAC mode handling.
- Removed unused AirzonecloudDaikinInstallation class.
### Changed
- Updated manifest.json version to 0.1.1.

## [0.1.0] - 2025-03-15
### Added
- Config Flow integration for DKN Cloud for HASS, enabling configuration via Home Assistant's UI.
- Validation for the scan_interval parameter (must be an integer ≥ 1, with a default value of 10 seconds) along with an informational message.
- Updated manifest.json with the new name "DKN Cloud for HASS", version set to 0.1.0, added "config_flow": true, and updated codeowners to include eXPerience83.
- Installation instructions added for HACS integration in the README.
- Updated issue tracker link to point to https://github.com/eXPerience83/DKNCloud-HASS/issues.
### Changed
- Removed the external dependency on AirzoneCloudDaikin by eliminating the "requirements" field from manifest.json.
- Updated HACS configuration in hacs.json to reflect the new project name "DKN Cloud for HASS".
- Updated repository URL references from "DKNCloud-HAS" to "DKNCloud-HASS".
- Forked from fitamix/DaikinDKNCloud-HomeAssistant (which itself is a fork of max13fr/Airzonecloud-HomeAssistant).
- Minor documentation updates to reflect the new configuration options and installation process.
### Fixed
- Added missing __init__.py to properly load the integration and avoid "No setup or config entry setup function defined" error.
