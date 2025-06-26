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
````

*Expected Response:* A JSON object containing the `"authentication_token"`.

---

### 2. Fetching Installations

To retrieve your installations, use:

```sh
curl -v "https://dkn.airzonecloud.com/installation_relations/?format=json&user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
```

*Note:* The response includes a list under `"installation_relations"`.

---

### 3. Fetching Devices

To get device details for a specific installation, use:

```sh
curl -v "https://dkn.airzonecloud.com/devices/?format=json&installation_id=YOUR_INSTALLATION_ID&user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
```

*The response contains device data (see anonymized example below).*

---

### 4. Mode Mapping (Px Options)

Airzone Cloud API defines several modes via "Px" options.
For practical use in this integration, focus on the following (others are generally ignored by most devices):

| P2 Value | Home Assistant HVAC Mode | Description                     |
| -------- | ------------------------ | ------------------------------- |
| `"1"`    | COOL                     | Cooling mode                    |
| `"2"`    | HEAT                     | Heating mode                    |
| `"3"`    | FAN\_ONLY                | Only ventilation (no heat/cool) |
| `"4"`    | HEAT\_COOL               | Heat/Cool (dual setpoint)\*     |
| `"5"`    | DRY                      | Dry/Dehumidify mode             |

\* The `HEAT_COOL` mode allows both heating and cooling setpoints and fan speeds to be adjusted.

Other Px modes (6, 7, 8) are not used in this integration.

---

**Control Commands**

* **P1:** Power On/Off.
* **P2:** Select HVAC mode (see table above).
* **P3:** Adjust fan speed in COOL and FAN\_ONLY modes.
* **P4:** Adjust fan speed in HEAT and HEAT\_COOL modes.
* **P7:** Set temperature for COOL mode (e.g., `"25.0"`).
* **P8:** Set temperature for HEAT mode and for the heat setpoint in HEAT\_COOL mode (e.g., `"23.0"`).
* **Slats and positions:** See device data below for vertical/horizontal slat state/positions.

**HEAT\_COOL Mode:**

* Setting the temperature in HEAT\_COOL mode sends *both* P7 and P8 (cold and heat consigns).
* Setting the fan speed in HEAT\_COOL mode sends *both* P3 and P4.
* Slats and positions can be observed and possibly controlled using their fields (see below).

---

### 5. Example Curl Commands for Device Control

Replace `YOUR_EMAIL@example.com`, `YOUR_TOKEN`, `YOUR_DEVICE_ID`, and `YOUR_INSTALLATION_ID` with your actual values or generic placeholders.

#### Power On (P1=1)

```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P1\", \"value\": 1}}"
```

#### Power Off (P1=0)

```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P1\", \"value\": 0}}"
```

#### Set Mode to HEAT (P2=2)

```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P2\", \"value\": \"2\"}}"
```

#### Set Mode to HEAT\_COOL (P2=4)

```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P2\", \"value\": \"4\"}}"
```

#### Set Temperature to 23°C in HEAT mode (P8)

```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P8\", \"value\": \"23.0\"}}"
```

#### Set Temperature to 25°C in COOL mode (P7)

```sh
curl -v "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL@example.com&user_token=YOUR_TOKEN" \
  -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
  -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P7\", \"value\": \"25.0\"}}"
```

#### Set Both Temperatures in HEAT\_COOL mode (P7 & P8)

```sh
# Set COOL setpoint (P7)
curl ... -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P7\", \"value\": \"22.0\"}}"
# Set HEAT setpoint (P8)
curl ... -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P8\", \"value\": \"25.0\"}}"
```

#### Set Fan Speed in HEAT\_COOL mode (P3 & P4)

```sh
# Set COOL fan speed (P3)
curl ... -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P3\", \"value\": 2}}"
# Set HEAT fan speed (P4)
curl ... -d "{\"event\": {\"cgi\": \"modmaquina\", \"device_id\": \"YOUR_DEVICE_ID\", \"option\": \"P4\", \"value\": 2}}"
```

---

### 6. Device Raw Data Example

Below is a fully anonymized sample device response with all slats and diagnostic fields included:

```json
{
    "id": "...",
    "mac": "AA:BB:CC:DD:EE:FF",
    "pin": "1234",
    "name": "MyDKNDevice",
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
    "spot_name": "LocationX",
    "complete_name": "LocationX,Region,Country",
    "location": {"latitude": 0.0, "longitude": 0.0}
}

```

---

**Notes:**

* The device’s `mac`, `pin`, `firmware`, and `brand` are key attributes for the Home Assistant device registry.
* Fields such as `ver_state_slats`, `ver_position_slats`, `hor_state_slats`, `hor_position_slats`, `ver_cold_slats`, `ver_heat_slats`, `hor_cold_slats`, and `hor_heat_slats` relate to slat positions/states for vertical and horizontal airflow control, if supported by your model.
* The `modes` field is a binary string indicating supported modes (positions 1–8; only the first five are relevant).
* Additional fields like `progs_enabled`, `scenary`, and `sleep_time` may be useful for diagnostics and future features.
* Always keep your real authentication token, device IDs, and installation IDs secret.
