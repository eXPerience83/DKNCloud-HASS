-------------------------------------------------------
Detailed Information on "Px" Modes and Example Curl Commands
-------------------------------------------------------

### 1. API Login and Token Retrieval
To authenticate with the Airzone Cloud API, use the following command:
```sh
curl -v -X POST "https://dkn.airzonecloud.com/users/sign_in" \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"email\": \"YOUR_EMAIL@example.com\", \"password\": \"YOUR_PASSWORD\"}"
```
*Expected Response:* A JSON object containing the `"authentication_token"`.

### 2. Fetching Installations
To retrieve your installations, use:
```sh
curl -v "https://dkn.airzonecloud.com/installation_relations/?format=json&user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
```
*Note:* The response includes a list under `"installation_relations"`.

### 3. Fetching Devices
To get device details for a specific installation, use:
```sh
curl -v "https://dkn.airzonecloud.com/devices/?format=json&installation_id=YOUR_INSTALLATION_ID&user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
```
The response contains device data (see example below).

### 4. Mode Mapping (Px Options)
Based on the original mapping from max13fr, the Airzone Cloud API defines several modes via the "Px" options:
```python
MODES_CONVERTER = {
    "1": {"name": "cool", "type": "cold", "description": "Cooling mode"},
    "2": {"name": "heat", "type": "heat", "description": "Heating mode"},
    "3": {"name": "ventilate", "type": "cold", "description": "Ventilation in cold mode"},
    "4": {"name": "heat-cold-auto", "type": "cold", "description": "Auto mode"},
    "5": {"name": "dehumidify", "type": "cold", "description": "Dry mode"},
    "6": {"name": "cool-air", "type": "cold", "description": "Automatic cooling"},
    "7": {"name": "heat-air", "type": "heat", "description": "Automatic heating"},
    "8": {"name": "ventilate", "type": "heat", "description": "Ventilation in heating mode"}
}
```
**In our tests (with the ADEQ125B2VEB model), only the following modes produced an effect:**
- **P1:** Power On/Off.
- **P2:** Mode selection:
  - `"1"` → COOL
  - `"2"` → HEAT
  - `"3"` → FAN ONLY
  - `"4"` → AUTO (forced via configuration)
  - `"5"` → DRY
- **P3:** Adjusts fan speed in COOL and FAN ONLY modes (valid values: 1, 2, 3).
- **P4:** Adjusts fan speed in HEAT/AUTO modes (valid values: 1, 2, 3).
- **P7:** Temperature setting for COOL mode (e.g., send `"25.0"`).
- **P8:** Temperature setting for HEAT/AUTO modes (e.g., send `"23.0"`).

For modes 6, 7, and 8 (if defined) the device should be considered as having those modes _unavailable_.

### 5. Example Curl Commands for Device Control Events
Replace `YOUR_EMAIL@example.com`, `YOUR_TOKEN`, `YOUR_DEVICE_ID`, and `YOUR_INSTALLATION_ID` with your actual values or generic placeholders.

- **Power On (P1=1):**
```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P1\", \"value\": 1}}"
```

- **Power Off (P1=0):**
```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P1\", \"value\": 0}}"
```

- **Change Mode to HEAT (P2=2):**
```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P2\", \"value\": \"2\"}}"
```

- **Force Auto Mode (HVACMode.AUTO) (P2=4):**
```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P2\", \"value\": \"4\"}}"
```

- **Set Temperature to 23°C in HEAT/AUTO mode (P8):**
```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P8\", \"value\": \"23.0\"}}"
```

- **Set Temperature to 25°C in COOL mode (P7):**
```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P7\", \"value\": \"25.0\"}}"
```

### 6. Device Raw Data Example
The following is an example of the raw device data returned by the API. This data is used to populate the device information in Home Assistant and may differ for each installation.
```json
{
    "id": "...",
    "mac": "AA:BB:CC:DD:EE:FF",
    "pin": "1234",
    "name": "Dknwserver",
    "status": "activated",
    "mode": "1",
    "state": null,
    "power": "0",
    "units": "0",
    "availables_speeds": "2",
    "local_temp": "26.0",
    "ver_state_slats": "0",
    "ver_position_slats": "0",
    "hor_state_slats": "0",
    "hor_position_slats": "0",
    "max_limit_cold": "32.0",
    "min_limit_cold": "16.0",
    "max_limit_heat": "32.0",
    "min_limit_heat": "16.0",
    "update_date": null,
    "progs_enabled": false,
    "scenary": "sleep",
    "sleep_time": 60,
    "min_temp_unoccupied": "16",
    "max_temp_unoccupied": "32",
    "connection_date": "2020-05-23T05:37:22.000+00:00",
    "last_event_id": "...",
    "firmware": "1.1.1",
    "brand": "Daikin",
    "cold_consign": "26.0",
    "heat_consign": "24.0",
    "cold_speed": "2",
    "heat_speed": "2",
    "machine_errors": null,
    "ver_cold_slats": "0001",
    "ver_heat_slats": "0000",
    "hor_cold_slats": "0000",
    "hor_heat_slats": "0000",
    "modes": "11101000",
    "installation_id": "...",
    "time_zone": "Europe/Madrid",
    "spot_name": "Madrid",
    "complete_name": "Madrid,Madrid,Community of Madrid,Spain",
    "location": {"latitude": 10.4155754, "longitude": -2.4037901998979576}
}

```

**Notes:**
- The device’s **MAC**, **PIN**, **firmware** (version), and **brand** (machine model) are key attributes that will be displayed in the device registry.
- The **modes** field is a binary string indicating support for each mode (positions 1 to 8). For our integration, the following mapping is used:
  - P2 "1" → COOL
  - P2 "2" → HEAT
  - P2 "3" → FAN ONLY
  - P2 "4" → AUTO
  - P2 "5" → DRY  
  Modes 6, 7, and 8 are treated as unavailable.
- Other fields like **progs_enabled**, **scenary**, and **sleep_time** provide additional information that may be used for future features, such as preset modes or scheduling.
