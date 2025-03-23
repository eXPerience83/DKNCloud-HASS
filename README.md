# DKN Cloud for HASS

DKN Cloud for HASS is a custom integration for Home Assistant that allows you to view and control your Daikin Airzone Cloud (dkn.airzonecloud.com) devices directly from Home Assistant.  
This fork is designed for the "DAIKIN ES.DKNWSERVER Wifi adapter."  
![Screenshot](https://github.com/eXPerience83/DKNCloud-HASS/blob/master/screenshot.png)

## Why this Fork?

This project is based on the original [max13fr/AirzonecloudDaikin-HomeAssistant](https://github.com/max13fr/AirzonecloudDaikin-HomeAssistant) package and has been adapted to add more functionality:
- Configuration via the Home Assistant UI.
- Support for additional entities, such as a temperature sensor and a power switch.
- Compatibility with Home Assistant 2025.3.
- Easy installation via HACS.
- Enhanced device information (including MAC, PIN, firmware version, and model) displayed in the device registry.
- Detailed API control over HVAC modes, temperature, and fan speed.

## Introduction

This integration uses an API client (in `airzone_api.py`) to:
- Authenticate via the `/users/sign_in` endpoint.
- Retrieve installations via the `/installation_relations` endpoint (using `user_email` and `user_token` as query parameters).
- Retrieve devices for each installation via the `/devices` endpoint.
- Send control events via the `/events` endpoint.

Additionally, the integration provides:
- A **climate platform** to control each air conditioner (power, HVAC mode, target temperature, and fan speed).
- A **sensor platform** to record the temperature probe (`local_temp`).
- A **switch platform** for device power control.

Basic control methods implemented in `climate.py` include:
- **turn_on:** Sends an event with P1=1.
- **turn_off:** Sends an event with P1=0.
- **set_hvac_mode:** Sends an event with P2. Supported mappings:
  - **HVACMode.OFF:** calls `turn_off()`.
  - **HVACMode.COOL:** sends P2=1.
  - **HVACMode.HEAT:** sends P2=2.
  - **HVACMode.FAN_ONLY:** sends P2=3.
  - **HVACMode.DRY:** sends P2=5.
  - **HVACMode.AUTO** (forced via configuration): sends P2=4.
- **set_temperature:** Sends an event with P8 for HEAT/AUTO modes or P7 for COOL mode, using the device’s temperature limits. The value is forced to be an integer with “.0” appended.
- **set_fan_speed:** Uses P3 to adjust fan speed in COOL and FAN_ONLY modes and P4 in HEAT/AUTO modes.

The API returns additional data (such as firmware, brand, available fan speeds, and temperature limits) that the integration uses:
- The field `availables_speeds` defines the valid fan speed options.
- The fields `min_limit_cold`, `max_limit_cold`, `min_limit_heat`, and `max_limit_heat` define the valid temperature ranges.
- All temperature values are sent as integers with “.0” appended (for example, 23 becomes “23.0”).

> **Important:**  
> Daikin climate equipment uses two consigns (one for heat and one for cold). Change the mode first (e.g., to heat) and then adjust the temperature. Although the original package defined modes up to "8", our tests indicate that only modes 1–5 produce an effect. Also note that fan speed commands differ for cold and heat modes.

## Installation

### Manual Installation
1. Create the `custom_components` folder in your Home Assistant configuration directory (if it doesn't already exist).
2. Copy the entire `airzoneclouddaikin` folder from this repository into the `custom_components` folder.
3. Restart Home Assistant.

### Installation via HACS
1. Open HACS in Home Assistant.
2. Go to **Integrations**.
3. Click the three-dot menu in the top right and select **Custom repositories**.
4. Enter the URL of this repository:  
   `https://github.com/eXPerience83/DKNCloud-HASS`
5. Set the category to **Integration**.
6. Click **Add**.
7. Search for "DKN Cloud for HASS" in HACS and install the integration.
8. Restart Home Assistant if prompted.

## Configuration

After installation, add the integration via the Home Assistant UI by going to **Settings > Devices & Services > Add Integration**, searching for "DKN Cloud for HASS", and following the prompts.

The configuration will ask for:
- **Username and Password:** Your Airzone Cloud account credentials.
- **Force HVAC Mode Auto:** (Optional checkbox) If enabled, the mode "auto" (HVACMode.AUTO) will be available for selection. Use this mode under your own responsibility.

## Usage

The integration retrieves your installations and devices, and creates:
- A **climate entity** for each device (allowing control of power, HVAC mode, target temperature, and fan speed).
- A **sensor entity** for the temperature probe (`local_temp`), which Home Assistant will record historically.
- A **switch entity** for device power control.

When you interact with these entities, the integration sends the corresponding events (P1, P2, P7, P8, and fan speed commands P3/P4) to the API. The sensor entity updates based on the device’s reported temperature.

## API Examples

For further testing, please refer to the `info.md` file for detailed information and example curl commands (using generic placeholders for sensitive data).

## Device Data

The API returns valuable information about each device that is used to populate the device registry in Home Assistant. For example, a device’s API response may include:
- **MAC:** The MAC address of the device.
- **PIN:** The device PIN.
- **Firmware:** The firmware version (e.g., "1.0.1").
- **Brand:** The machine model (e.g., "ADEQ125B2VEB").
- **Modes:** A binary string (e.g., "11101000") indicating the supported modes.  
  The supported mode mapping is as follows:  
  - **P2 "1" → COOL**  
  - **P2 "2" → HEAT**  
  - **P2 "3" → FAN ONLY**  
  - **P2 "4" → AUTO**  
  - **P2 "5" → DRY**  
  Modes 6–8 are treated as unavailable.
- **Progs_enabled:** Indicates if scheduling or programming is enabled.
- **Scenary:** Indicates the preset mode (e.g., "occupied", "vacant", "sleep").
- **Sleep_time:** Shows the auto-off time for the preset "sleep" mode.

## Donations

If you find this integration useful and would like to support further development, please consider donating:
- [PayPal](https://paypal.me/eXPerience83)
- [Ko-fi](https://ko-fi.com/experience83)

## License

This project is licensed under the MIT License.
