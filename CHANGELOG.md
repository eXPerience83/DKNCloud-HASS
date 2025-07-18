# Changelog

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
