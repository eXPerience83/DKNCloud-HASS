# DKN Cloud for HASS

DKN Cloud for HASS is a custom integration for Home Assistant that allows you to view and control your Daikin Airzone Cloud (dkn.airzonecloud.com) devices directly from Home Assistant.  
This fork is designed for the "DAIKIN ES.DKNWSERVER Wifi adapter."  
![Screenshot](https://github.com/eXPerience83/DKNCloud-HASS/blob/master/screenshot.png)

## Why this Fork?

This project is based primarily on the original [AirzoneCloudDaikin](https://pypi.org/project/AirzoneCloudDaikin/) package by [max13fr](https://github.com/max13fr/AirzoneCloudDaikin) and its Home Assistant adaptation. In this fork we have:
- Added configuration via the Home Assistant UI.
- Supported additional entities (including a temperature sensor and a power switch).
- Improved device information display (MAC, PIN, firmware version, and model).
- Enhanced HVAC mode control, including forced auto mode.
- Made the integration compatible with Home Assistant 2025.3.
- Simplified installation via HACS.

For more detailed API information—including the original mode mapping from max13fr—please refer to the [info.md](./info.md) file.

## Introduction

This integration uses an API client (in `airzone_api.py`) to:
- **Authenticate** via the `/users/sign_in` endpoint.
- **Retrieve installations** via the `/installation_relations` endpoint (using `user_email` and `user_token` as query parameters).
- **Retrieve devices** for each installation via the `/devices` endpoint.
- **Send control events** via the `/events` endpoint.

Additionally, the integration provides:
- A **climate platform** to control each air conditioner (power, HVAC mode, target temperature, and fan speed).
- A **sensor platform** to record the temperature probe (`local_temp`).
- A **switch platform** for device power control.

## Key Features

- **Climate Entity**  
  Control methods implemented in `climate.py` include:
  - **turn_on:** Sends an event with P1=1.
  - **turn_off:** Sends an event with P1=0.
  - **set_hvac_mode:** Sends an event with P2. Supported mappings:
    - HVACMode.OFF: calls `turn_off()`.
    - HVACMode.COOL: sends P2=1.
    - HVACMode.HEAT: sends P2=2.
    - HVACMode.FAN_ONLY: sends P2=3.
    - HVACMode.DRY: sends P2=5.
    - HVACMode.AUTO (if forced via configuration): sends P2=4.
  - **set_temperature:** Uses P8 for HEAT/AUTO modes or P7 for COOL mode. Temperature values are constrained to device limits and sent as an integer with “.0” appended.
  - **set_fan_speed:** Uses P3 to adjust fan speed in COOL and FAN_ONLY modes and P4 in HEAT/AUTO modes.

- **Sensor Entity**  
  A sensor for the temperature probe (`local_temp`) is created to record current temperature values (in °C). This sensor is updated based on the API’s response.

- **Switch Entity**  
  A switch entity is provided for power control, sending P1 events to turn the device on or off.

- **Enhanced Device Information**  
  Additional details such as MAC address, PIN, firmware version, and model (brand) are extracted from the API and shown in the device registry.

> **Important:**  
> Daikin climate equipment uses two consigns (one for heat and one for cold). Change the mode first (e.g., to heat) and then adjust the temperature. Although the original package defined modes up to "8", our tests indicate that only modes 1–5 produce an effect. For further details on mode mappings and example curl commands, see [info.md](./info.md).

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

After installation, add the integration via the Home Assistant UI by navigating to **Settings > Devices & Services > Add Integration**, searching for "DKN Cloud for HASS", and following the prompts.

The configuration will ask for:
- **Username and Password:** Your Airzone Cloud account credentials.
- **Force HVAC Mode Auto:** (Optional checkbox) If enabled, the mode "auto" (HVACMode.AUTO) will be available for selection. Use this mode under your own responsibility.

## Usage

The integration retrieves your installations and devices, and creates:
- A **climate entity** for each device (allowing control of power, HVAC mode, target temperature, and fan speed).
- A **sensor entity** for the temperature probe (`local_temp`), with historical data storage.
- A **switch entity** for device power control.

When you interact with these entities, the integration sends the corresponding events (P1, P2, P7, P8, and fan speed commands P3/P4) to the API. The sensor entity updates based on the device’s reported temperature.

## API Examples

For further testing, please refer to the [info.md](./info.md) file for detailed information and example curl commands (using generic placeholders for sensitive data).

## Donations

If you find this integration useful and would like to support its development, please consider donating:
- [PayPal](https://paypal.me/eXPerience83)
- [Ko-fi](https://ko-fi.com/experience83)

## License

This project is licensed under the MIT License.
