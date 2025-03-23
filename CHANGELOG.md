# Changelog

All notable changes to this project will be documented in this file.

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
