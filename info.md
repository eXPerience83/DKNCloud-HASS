# DKN Cloud for HASS — Technical Reference

> **Scope:** Single source of truth for the DKN (Airzone Cloud) behavior we rely on in the Home Assistant integration.  
> **Goal:** Be precise, device-agnostic where needed, and conservative when behavior varies by model/firmware.

---

## 1) Base URL, Auth, and General Request Pattern

- **Base URL:** `https://dkn.airzonecloud.com/`
- **Auth model:** `POST /users/sign_in` returns an **authentication token**. Subsequent requests include:
  - `?user_email=<EMAIL>&user_token=<TOKEN>` as **query parameters** (and usually `format=json` for JSON responses).
- **Privacy:** Never log or expose `user_email`, `user_token`, `mac`, `pin`, GPS coordinates, or related PII. Always sanitize logs.

**Canonical request shape**
```

GET/POST/PUT/DELETE <PATH>?user_email=<EMAIL>&user_token=<TOKEN>[&format=json]
Content-Type: application/json
User-Agent: (browser-like UA)
X-Requested-With: XMLHttpRequest
Accept: application/json, text/plain, */*

````

---

## 2) Core Resources and Endpoints

- **Login**
  - `POST /users/sign_in` → returns `authentication_token`.

- **Installations / Relations**
  - `GET  /installation_relations/`
  - `GET  /installations/`
  - `GET  /installations/<id>`
  - `POST /installation_relations`
  - `PUT  /installation_relations/<id>`
  - `DELETE /installation_relations/<id>`

- **Devices**
  - `GET  /devices/?installation_id=<id>` → list devices
  - `GET  /devices/<id>` → device detail
  - `POST /devices` → add device (needs `mac` + `pin`)
  - `PUT  /devices/<id>` → update **device fields** (not real-time events), notably:
    - `device.scenary`: `"occupied" | "vacant" | "sleep"`
    - `sleep_time`: integer minutes (see §6)
    - **Unoccupied limits** when present: `min_temp_unoccupied`, `max_temp_unoccupied` (see §6)

- **Real-time Control**
  - `POST /events/` with body:
    ```json
    {
      "event": {
        "cgi": "modmaquina",
        "option": "P<NN>",
        "value": "<value>",
        "device_id": "<id>"
      }
    }
    ```

- **Users**
  - `DELETE /users/sign_out`

- **Schedules**
  - `GET/POST/PUT/DELETE /schedules/` (CRUD exists; **kept for future implementation**).

> **Error handling:** 401 means credentials invalid/expired; the integration should refresh/revalidate. Rate limits (429) should trigger retry with backoff; see §10.

---

## 3) Event Model (Px options) — Consolidated Table

> **Value formats:** Numbers are commonly sent as **strings**. For setpoints, devices accept **numeric strings with one decimal** (e.g., `"25.0"`), though resolution is effectively **integer °C**.

| P-code | Name / Purpose                 | Value (examples)     | Notes (routing/rules)                                       | Implemented in HA |
|:-----:|---------------------------------|----------------------|--------------------------------------------------------------|:-----------------:|
| P1    | Power                           | `"0"` / `"1"`        | Off/On                                                       | ✅ |
| P2    | HVAC mode                       | `"1"`..`"8"`         | See §4 mapping and exposure policy                           | ✅ (with policy) |
| P3    | Fan speed (COLD-type modes)     | `"1"`..`"5"`         | Use when current mode is **COLD-type** (see §4)              | ✅ |
| P4    | Fan speed (HEAT-type modes)     | `"1"`..`"5"`         | Use when current mode is **HEAT-type** (see §4)              | ✅ |
| P7    | COOL setpoint                   | `"16.0"`..`"32.0"`   | Integer °C semantics; send with `.0`                         | ✅ |
| P8    | HEAT setpoint                   | `"16.0"`..`"32.0"`   | Integer °C semantics; send with `.0`                         | ✅ |
| P9    | Vertical slats (cold)           | model-dependent      | Advanced; **not planned**                                    | ❌ (unplanned) |
| P10   | Vertical slats (heat)           | model-dependent      | Advanced; **not planned**                                    | ❌ (unplanned) |
| P19   | Horizontal slats (cold)         | model-dependent      | Advanced; **not planned**                                    | ❌ (unplanned) |
| P20   | Horizontal slats (heat)         | model-dependent      | Advanced; **not planned**                                    | ❌ (unplanned) |

> Additional P-codes may exist in the platform but are **undocumented for our purposes**. When we confirm their semantics, we'll add them here. For now they are **not planned**.

---

## 4) HVAC Modes (P2) — Mapping and Exposure Policy

**Ordered list for P2 (value `"1"`..`"8"`):**
````

["cool", "heat", "ventilate", "heat-cold-auto", "dehumidify", "cool-air", "heat-air", "ventilate"]

```

**Mode classification (drives fan routing)**
```

cold_modes = {1,3,4,5,6}
heat_modes = {2,7,8}

````

### Exposure in Home Assistant (default + opt-in)

| P2 | Label            | Expose in HA | Temp control (if any) | Fan control | Notes |
|:--:|------------------|:------------:|------------------------|-------------|-------|
| 1  | cool             | **Yes**      | COOL → **P7**          | **P3**      | Default COOL. |
| 2  | heat             | **Yes**      | HEAT → **P8**          | **P4**      | Default HEAT. |
| 3  | ventilate        | **Yes**      | **N/A**                | **P3**      | `fan_only` default if supported. |
| 4  | heat-cold-auto   | **Opt-in**   | Typically COOL → **P7*** | **P3***   | Device-dependent/untested; not enabled by default. |
| 5  | dehumidify       | **Yes**      | **N/A**                | **N/A**     | `dry`. No target temp and **no fan control**. |
| 6  | cool-air         | **No**       | Unknown/tentative      | Unknown     | **Not exposed**; semantics unclear for our models. |
| 7  | heat-air         | **No**       | Unknown/tentative      | Unknown     | **Not exposed**; semantics unclear for our models. |
| 8  | ventilate        | **Yes** (fallback) | **N/A**          | **P4**      | Use only if 3 unsupported and 8 supported (see below). |

\* For `P2=4 (heat-cold-auto)`, until broader validation: treat as **cold-type** for fan (**P3**) and setpoint (**P7**) by default. It remains **opt-in** and device-dependent.

**Ventilate selection policy (P2=3 vs P2=8)**
- If **3** and **8** are both supported: expose **`fan_only`** using **P2=3** (default).
- If **3** is **not** supported but **8** **is**: expose **`fan_only`** using **P2=8**.
- If neither **3** nor **8** is supported: do **not** expose `fan_only`.
- While in **P2=8**, fan routing follows **HEAT-type** classification (**P4**), unless device telemetry proves otherwise for a specific model.

**Setpoint availability**
- **No target temperature** in `fan_only` (P2=3 or 8) and `dry` (P2=5).
- Otherwise: COOL-type → **P7**, HEAT-type → **P8** (see table).

**Fan “auto” note**
- No confirmed user-facing **fan auto**. `availables_speeds` governs allowed speeds.  
- A future **virtual “auto”** may be added (heuristic), but it would not represent a real device control.

---

## 5) Supported Modes Bitmask (`modes`)

Devices expose a **string** bitmask for 8 modes, **index-aligned with P2**:

| Index (0-based) | P2 value | Label            |
|:---------------:|:--------:|------------------|
| 0               | 1        | cool             |
| 1               | 2        | heat             |
| 2               | 3        | ventilate        |
| 3               | 4        | heat-cold-auto   |
| 4               | 5        | dehumidify       |
| 5               | 6        | cool-air         |
| 6               | 7        | heat-air         |
| 7               | 8        | ventilate        |

**Integration:** compute supported HA modes from this bitmask + exposure policy in §4. Apply the `ventilate` selection policy above.

---

## 6) Scenes (`scenary`), Sleep **& Unoccupied Limits**

- **Scene (`scenary`)**: `"occupied" | "vacant" | "sleep"`  
  - Update via: `PUT /devices/<id>` with body `{"device":{"scenary":"occupied"}}`
- **Sleep (`sleep_time`)**: minutes in **[30..120]**, **step 10**.
  - Update via: `PUT /devices/<id>` with body `{"device":{"sleep_time":60}}`

### Unoccupied limits (when provided by the backend)
Two device fields may appear and can be updated via **root-level** PUT on `/devices/<id>`:

- `min_temp_unoccupied` (**HEAT** minimum): **12..22 °C**, step **1**
- `max_temp_unoccupied` (**COOL** maximum): **24..34 °C**, step **1**
  - Send updates inside the device wrapper, e.g. `{"device":{"min_temp_unoccupied":18}}` (you may combine multiple fields).

**Occupied switching note:** When sending control events (power/mode/speed/setpoint), the backend **often switches** a `vacant` device to `occupied`. The integration **does not force** `occupied` proactively. We **refresh** after commands; if a specific installation requires it, a **config option** may force `scenary="occupied"` (with a short cooldown).

---

## 7) Temperatures, Units and Precision

- Practical resolution is **integer °C**, but send values as **numeric strings with `.0`**, e.g., `"25.0"`, `"21.0"`.
- COOL setpoints use **P7**; HEAT setpoints use **P8**.
- Do **not** expose/allow target temperature in `fan_only` and `dry`.

---

## 8) Fan Speeds

- Use **P3** for COLD-type modes; **P4** for HEAT-type modes.
- Respect `availables_speeds` and the current mode’s type when setting fan.
- **Dehumidify (P2=5)** does **not** support fan speeds.

---

## 9) Example cURL (sanitized)

> Replace: `YOUR_EMAIL`, `YOUR_PASSWORD`, `YOUR_TOKEN`, `YOUR_INSTALLATION_ID`, `YOUR_DEVICE_ID`

### 9.1 Login (get token)
```sh
curl -X POST "https://dkn.airzonecloud.com/users/sign_in" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"email":"YOUR_EMAIL","password":"YOUR_PASSWORD"}'
````

### 9.2 List installation relations

```sh
curl "https://dkn.airzonecloud.com/installation_relations/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest"
```

### 9.3 List devices of an installation

```sh
curl "https://dkn.airzonecloud.com/devices/?installation_id=YOUR_INSTALLATION_ID&user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest"
```

### 9.4 Get a device

```sh
curl "https://dkn.airzonecloud.com/devices/YOUR_DEVICE_ID?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest"
```

### 9.5 Power ON / OFF (P1)

```sh
# ON
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P1","value":"1"}}'

# OFF
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P1","value":"0"}}'
```

### 9.6 Set HVAC Mode (P2)

```sh
# COOL (prefer 1)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P2","value":"1"}}'

# HEAT
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P2","value":"2"}}'

# FAN_ONLY (prefer 3; fallback 8 if 3 unsupported)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P2","value":"3"}}'

# DRY
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P2","value":"5"}}'

# HEAT_COOL / AUTO (opt-in; device-dependent)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P2","value":"4"}}'
```

### 9.7 Setpoint (P7/P8)

```sh
# COOL setpoint to 25.0°C (P7)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P7","value":"25.0"}}'

# HEAT setpoint to 21.0°C (P8)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P8","value":"21.0"}}'
```

### 9.8 Fan speed (P3/P4)

```sh
# Set fan speed "2" while in a COLD-type mode (uses P3)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P3","value":"2"}}'

# Set fan speed "2" while in a HEAT-type mode (uses P4)
curl -X POST "https://dkn.airzonecloud.com/events/?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"event":{"cgi":"modmaquina","device_id":"YOUR_DEVICE_ID","option":"P4","value":"2"}}'
```

### 9.9 Scene, Sleep & Unoccupied Limits (PUT /devices/<id>)

```sh
# Set scenary to 'occupied'
curl -X PUT "https://dkn.airzonecloud.com/devices/YOUR_DEVICE_ID?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"device":{"scenary":"occupied"}}'

# Set sleep_time to 60 (valid: 30..120, step 10)
curl -X PUT "https://dkn.airzonecloud.com/devices/YOUR_DEVICE_ID?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"sleep_time":60}'

# Set unoccupied HEAT minimum (12..22)
curl -X PUT "https://dkn.airzonecloud.com/devices/YOUR_DEVICE_ID?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"min_temp_unoccupied":18}'

# Set unoccupied COOL maximum (24..34)
curl -X PUT "https://dkn.airzonecloud.com/devices/YOUR_DEVICE_ID?user_email=YOUR_EMAIL&user_token=YOUR_TOKEN&format=json" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/plain, */*" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"max_temp_unoccupied":28}'
```

---

## 10) Client Robustness (Integration)

* **Timeouts:** `ClientTimeout(total=30s)` for all HTTP calls.
* **429 / 5xx / timeouts:** Exponential backoff **with jitter**, plus a short **global cooldown** after 429 (respect `Retry-After` when present).
* **401:** Single **re-login and retry once** on the first 401; then surface the error.
* **No I/O in entity properties**; only in the `DataUpdateCoordinator`.
* **Startup errors:** raise `ConfigEntryNotReady` when the API is unreachable.
* **Privacy:** sanitize logs and diagnostics; never include full URLs with credentials.

---

## 11) Device State — Fields of Interest (Non-PII)

* `power`, `mode` (P2 index as string), `local_temp`, `cold_consign`, `heat_consign`
* `cold_speed`, `heat_speed`, `availables_speeds`
* `scenary`, `sleep_time`
* Limits: `min_limit_cold`, `max_limit_cold`, `min_limit_heat`, `max_limit_heat`
* `min_temp_unoccupied`, `max_temp_unoccupied`
* `modes` bitmask string (see §5)

> **Never** expose: `user_email`, `authentication_token`, `mac`, `pin`, GPS `location`.

---

## 12) Implementation Matrix (current plan)

| Feature                                   | Implemented | Notes                                               |
| ----------------------------------------- | :---------: | --------------------------------------------------- |
| Power (P1)                                |      ✅      | switch + climate                                    |
| HVAC mode (P2)                            |      ✅      | Default: COOL/HEAT/FAN_ONLY/DRY; optional HEAT_COOL |
| Fan speed (P3/P4)                         |      ✅      | Routed by mode type (cold vs heat)                  |
| Setpoint COOL/HEAT (P7/P8)                |      ✅      | Send as `"NN.0"` strings                            |
| **Select: scenary**                       |      ✅      | `PUT /devices/<id>`                                 |
| **Number: sleep_time (30..120, step 10)** |      ✅      | `PUT /devices/<id>`                                 |
| **Number: min/max unoccupied**            |      ✅      | Optional: appears when backend provides the fields  |
| Schedules CRUD                            |      ❌      | Roadmap                                             |
| Heat_cool exposed by default              |      ❌      | Optional, user opt-in only                          |

---

## 13) Open Questions / Next Validation

* Clarify behavior of **P2=4 (auto)** and **“air” variants (6/7)** and **ventilate (8)** across models.
* Finalize scheduling payload + UI mapping if we implement schedules.
