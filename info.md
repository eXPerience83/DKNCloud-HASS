# DKN Cloud for HASS — Technical Reference

> **Scope:** Single source of truth for the DKN (Airzone Cloud) backend behavior that the Home Assistant integration relies on.  
> **Evidence:** Verified via the official DKN Cloud UI (web/mobile) behavior and observed backend responses from manual/API tests.  
> **Goal:** Be precise, conservative when behavior varies by device/firmware, and clearly separate **backend contract** vs **UI constraints**.

---

## 1) Base URL, Auth, and General Request Pattern

- **Base URL:** `https://dkn.airzonecloud.com/`
- **Auth model:** `POST /users/sign_in` returns an authentication token.
- Subsequent requests pass credentials as **query parameters**:
  - `user_email=<EMAIL>`
  - `user_token=<TOKEN>`
  - Many endpoints also accept/require `format=json`.

### Canonical request shape

**URL pattern**
```
<BASE>/<PATH>?user_email=<EMAIL>&user_token=<TOKEN>[&format=json][&installation_id=<ID>]
```

**Typical headers (browser-like)**
- `Accept: application/json, text/plain, */*`
- `Content-Type: application/json;charset=UTF-8` (for JSON writes)
- `X-Requested-With: XMLHttpRequest`
- `User-Agent: <browser-like UA>` (optional but helps match the official UI)

---

## 2) Core Resources and Endpoints

### Login
- `POST /users/sign_in`
- Body: `{"email":"...","password":"..."}`
- **Success codes observed:** 200, 201

### Installations / relations
- `GET /installation_relations?user_email=...&user_token=...&format=json`
- Returns a list of relations; **use `installation_id`** (not the relation `id`) to query devices.

### Devices snapshot (preferred)
- `GET /devices?format=json&installation_id=<installation_id>&user_email=...&user_token=...`
- This is the most reliable way to poll state during tests and for integration “refresh”.

> Note: In some environments, GET /devices/<id> has been observed to be less stable (intermittent 5xx). Prefer the snapshot list endpoint when possible.

### Updating device fields — `PUT /devices/<id>`

- `PUT /devices/<device_id>?format=json&user_email=...&user_token=...`
- **Success codes observed:** 200, 204
- **Response body:** 204 responses typically include an empty body; clients should
  treat empty JSON/text bodies as `None`/`""` and still consider the request
  successful.
- **Canonical payload shapes (aligned to the official UI):**
  - `scenary` is sent **nested** under `device`.
  - Most other “simple fields” are sent **root-level** (top-level JSON).

#### Canonical `PUT` examples

**Scene (scenary)**
```json
{ "device": { "scenary": "occupied" } }
```

**Sleep timer**
```json
{ "sleep_time": 30 }
```

**Unoccupied limits**
```json
{ "min_temp_unoccupied": 18 }
{ "max_temp_unoccupied": 28 }
```

> The backend may accept alternative shapes for some fields, but the integration should use the canonical shapes above to align with the official UI.

### Real-time control — `POST /events`

- `POST /events?user_email=...&user_token=...`
- Body:
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
- **Success codes observed:** 200, 201, 204  
  Treat **any 2xx** as success.


### Curl examples (copy/paste templates)

> Replace placeholders (`<...>`). Keep credentials out of logs.

```bash
BASE="https://dkn.airzonecloud.com"
EMAIL="<URLENCODED_EMAIL>"
TOKEN="<TOKEN>"
INSTALLATION_ID="<INSTALLATION_ID>"
DEVICE_ID="<DEVICE_ID>"

# 1) List installations/relations
curl -sS --compressed \
  -H "Accept: application/json, text/plain, */*" \
  "$BASE/installation_relations?user_email=$EMAIL&user_token=$TOKEN&format=json"

# 2) Snapshot devices (recommended polling endpoint)
curl -sS --compressed \
  -H "Accept: application/json, text/plain, */*" \
  "$BASE/devices?format=json&installation_id=$INSTALLATION_ID&user_email=$EMAIL&user_token=$TOKEN"

# 3) Set scenary (nested under device)
curl -sS --compressed \
  -X PUT \
  -H "Accept: application/json, text/plain, */*" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "X-Requested-With: XMLHttpRequest" \
  --data-raw '{ "device": { "scenary": "occupied" } }' \
  "$BASE/devices/$DEVICE_ID?format=json&user_email=$EMAIL&user_token=$TOKEN"

# 4) Set sleep_time (root-level)
curl -sS --compressed \
  -X PUT \
  -H "Accept: application/json, text/plain, */*" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "X-Requested-With: XMLHttpRequest" \
  --data-raw '{ "sleep_time": 30 }' \
  "$BASE/devices/$DEVICE_ID?format=json&user_email=$EMAIL&user_token=$TOKEN"

# 5) Control example: set mode COOL (P2=1) via /events
curl -sS --compressed \
  -X POST \
  -H "Accept: application/json, text/plain, */*" \
  -H "Content-Type: application/json;charset=UTF-8" \
  -H "X-Requested-With: XMLHttpRequest" \
  --data-raw '{ "event": { "cgi":"modmaquina","device_id":"'"$DEVICE_ID"'","option":"P2","value":"1" } }' \
  "$BASE/events?user_email=$EMAIL&user_token=$TOKEN"
```


---

## 3) Event Model (Px options) — Consolidated Table

> **Value formats:** The backend commonly accepts numeric values as strings. Setpoints are typically sent as strings with one decimal.

| P-code | Purpose                         | Value examples          | Notes |
|:-----:|----------------------------------|-------------------------|------|
| P1    | Power                            | `"0"`, `"1"`            | Off/On |
| P2    | HVAC mode                         | `"1"`..`"8"`            | Mapping in §4 |
| P3    | Fan speed (cold-type modes)       | `"0"`..`"<N>"`          | `"0"` = **Auto fan** when supported (see §8) |
| P4    | Fan speed (heat-type modes)       | `"0"`..`"<N>"`          | `"0"` = **Auto fan** when supported (see §8) |
| P7    | COOL setpoint                     | `"16.0"`..`"32.0"`      | UI range; backend may accept out-of-range (see §7) |
| P8    | HEAT setpoint                     | `"16.0"`..`"32.0"`      | UI range; backend may accept out-of-range (see §7) |
| P9    | Vertical slats (cold)             | model-dependent         | Advanced; not implemented by the integration (for now) |
| P10   | Vertical slats (heat)             | model-dependent         | Advanced; not implemented by the integration (for now) |
| P19   | Horizontal slats (cold)           | model-dependent         | Advanced; not implemented by the integration (for now) |
| P20   | Horizontal slats (heat)           | model-dependent         | Advanced; not implemented by the integration (for now) |

---

## 4) HVAC Modes (P2) — Mapping, Classification, and UI Equivalences

### 4.1 P2 value → label (as presented in the official UI)

| P2 | Label (UI) | Notes |
|:--:|-----------|------|
| 1 | COOL | |
| 2 | HEAT | |
| 3 | FAN_ONLY / VENTILATE | No target temperature |
| 4 | HEAT_COOL / AUTO (heat-cold-auto) | Only if supported by `modes` bitmask; not enabled by default in the integration |
| 5 | DRY | No target temperature; fan control is typically not exposed |
| 6 | (reported) heat-cold-auto (alias) | Some devices report this in telemetry |
| 7 | (reported) heat-cold-auto (alias) | Some devices report this in telemetry |
| 8 | (reported) ventilate (alias) | Some devices report this in telemetry |

### 4.2 Ordered list (P2 values 1..8)

This ordered list is useful as a stable, index-based mapping of the `mode` integer:

```json
["cool", "heat", "ventilate", "heat_cool", "dehumidify", "cool-air", "heat-air", "ventilate"]
```

Notes:
- Entries **6/7/8** are commonly observed as *backend/telemetry aliases* (see §4.4). Do not assume they are user-selectable.
- `ventilate` appears twice (P2=3 and P2=8) because some installations report **8** as an alias for fan/ventilation.

### 4.3 Mode classification (drives fan routing)

Some backend fields and writes are effectively split into “cold-side” vs “heat-side” fan channels:

```py
cold_modes = {1, 3, 4, 5, 6}   # COOL, VENTILATE, HEAT_COOL, DRY, cool-air
heat_modes = {2, 7, 8}         # HEAT, heat-air, (ventilate alias)
```

Practical guidance for the integration:
- **Fan speed writes:**  
  - If `mode ∈ cold_modes` → write via **P3** and read `cold_speed` (when fan control is applicable).  
  - If `mode ∈ heat_modes` → write via **P4** and read `heat_speed` (when fan control is applicable).
- **Applicability caveat:** even if a mode belongs to a routing set, the official UI may not expose **fan or setpoint** controls in that mode:
  - **FAN_ONLY / VENTILATE (P2=3 or alias 8):** no setpoints; **fan control is exposed** (speed/auto depending on device support).
  - **DRY / DEHUMIDIFY (P2=5):** no setpoints; **fan control is typically not exposed** in the official UI.
- To stay conservative, the integration should actively write fan speeds in modes where fan control is exposed and known to be effective (**COOL, HEAT, and FAN_ONLY/VENTILATE**), and avoid forcing fan writes in **DRY/DEHUMIDIFY**. For **HEAT_COOL**, only allow fan writes when explicitly enabled and validated for the installation.

### 4.4 Observed “alias” / stabilization behavior (important)

- **P2=3 vs P2=8:** In some installations, after writing **P2=3** the backend can **temporarily report `mode=8`**, then later stabilize back to `mode=3`.  
  **Guidance:** treat **8 as FAN_ONLY-equivalent for display/state**, and avoid writing P2=8 in normal operation.

  **Practical note:** forcing unsupported/alias modes can confuse some UIs until a valid mode is written again. Prefer restoring to a supported mode from the `modes` bitmask.

- **P2=6/7:** Some devices can report 6/7 in telemetry; these are best treated as **HEAT_COOL-equivalent for display/state** unless explicitly validated for control.

### 4.5 Exposure policy (Home Assistant)

Conservative default policy:
- Expose only confirmed modes: **COOL, HEAT, FAN_ONLY, DRY**.
- Do not expose HEAT_COOL/AUTO unless it is explicitly enabled and validated per-device/installation.

---

## 5) Supported Modes Bitmask (`modes`)

Devices expose a **bitmask string** aligned with P2 (index 0 corresponds to P2=1).

Example: `11101000` means P2 values **1,2,3,5** are supported.

| Bit index | P2 | Meaning |
|:--------:|:--:|---------|
| 0 | 1 | COOL |
| 1 | 2 | HEAT |
| 2 | 3 | FAN_ONLY |
| 3 | 4 | HEAT_COOL / AUTO |
| 4 | 5 | DRY |
| 5 | 6 | (reported) alias |
| 6 | 7 | (reported) alias |
| 7 | 8 | (reported) alias |

---

## 6) Scenes (`scenary`), Sleep, and Unoccupied Limits

### 6.1 Scene (`scenary`)

Known values:
- `"occupied"`
- `"vacant"`
- `"sleep"`

**Common UI behavior (important):** when the unit is `"vacant"`, the official UI often switches it to `"occupied"` before applying user actions (mode changes, fan changes, slats changes, etc.).  
This means integrations should be prepared for `vacant → occupied` writes in the flow of normal control.

**Canonical write**
```json
{ "device": { "scenary": "sleep" } }
```

### 6.2 Sleep (`sleep_time`)

- Field: `sleep_time` (minutes)
- Canonical write uses **root-level** `PUT /devices/<id>`.

**UI constraints**
- UI typically offers **30..120** minutes in steps of **10** (device UI constraint).

**Contract note**
- The backend can accept values outside those constraints (observed examples include 12, 35, 183).
- Do not rely on out-of-range acceptance; if the backend accepts out-of-range in some environments, treat that as non-contractual.

### 6.3 Unoccupied limits (device-level)

Fields:
- `min_temp_unoccupied` (HEAT min)
- `max_temp_unoccupied` (COOL max)

**Typical per-device UI limits**
- `min_temp_unoccupied`: **12..22 °C**, step **1**
- `max_temp_unoccupied`: **24..34 °C**, step **1**

**Bulk-edit UI note:** some UI screens apply one value to all devices and can show different constraints (commonly **16..32**). Treat that as a UI constraint, not necessarily the per-device contract.

**Canonical writes**
```json
{ "min_temp_unoccupied": 18 }
{ "max_temp_unoccupied": 28 }
```

---

## 7) Temperatures, Units, Precision, and Availability

### 7.1 Units and precision

- Backend setpoints are effectively **Celsius** and are typically sent as **strings with one decimal**:
  - `"25.0"`, `"21.0"`, etc.
- Clients operating in Fahrenheit are expected to convert to Celsius **before** sending setpoints.

**Integration note (Home Assistant):**
- Home Assistant can be configured to Fahrenheit; the integration must convert any Fahrenheit setpoint into Celsius before sending P7/P8.

### 7.2 Setpoints (P7/P8) and ranges

- **UI range:** 16.0..32.0 °C (commonly displayed in the official UI).
- **Out-of-range note:** some environments may accept out-of-range writes via the API, but the physical unit and/or backend may clamp or reject them. Treat out-of-range acceptance as **non-contractual**.

### 7.3 Setpoint availability by mode

- **FAN_ONLY (P2=3):** no target temperature (P7/P8 not meaningful).
- **DRY (P2=5):** no target temperature; fan control is typically not exposed in the UI.

---

## 8) Fan Speeds and “Auto Fan” (real device feature)

### 8.1 Manual speeds

- The device reports `availables_speeds` (N).  
  Manual fan values are typically `1..N` and are mode-dependent:
  - COOL-type modes use `cold_speed` and P3 writes
  - HEAT-type modes use `heat_speed` and P4 writes

### 8.2 Auto fan (P3/P4 value `"0"`)

- The official UI supports a **real Auto fan** mode on some devices/firmwares:
  - Write `"0"` to P3 (cool-type) or P4 (heat-type).
- Auto fan availability is device/firmware dependent.

**UI heuristic (non-contractual):** some clients only display “Auto” when firmware information suggests support (e.g., firmware ending in a digit > 2).  
Treat this as a UI heuristic; **the backend contract is simply whether the device accepts/stores `"0"` for P3/P4**.

**Future optional feature (integration):**
- A separate, opt-in *virtual* “Auto Fan” can be implemented for devices that **do not** support real Auto (heuristic control). This is **not** the same as device Auto and must be clearly labeled/opt-in.

---

## 9) Error Semantics Worth Handling

- `/events` may respond 2xx even if the physical unit later clamps/corrects the value.
- `/events` may return 422 when the WServer is not connected.
- `/events` may return 423 when the device/machine is not ready.
- Some changes propagate with delay; always prefer polling `/devices?...` to confirm settled state.
- Treat repeated scenary writes (e.g., `sleep → occupied`) as **idempotent** and safe.

---

## 10) Backend Robustness Expectations (Integration)

- Use canonical payload shapes (§2) to align with the official UI.
- Treat **any 2xx** from `/events` as success; do not assume immediate state reflects the write.
- Prefer device snapshots (`GET /devices?...`) for verification and diagnostics.

### Sleep session behavior and cleanup (integration behavior)

Observed behavior:
- After `sleep_time` elapses, devices typically power off but may keep reporting `scenary="sleep"` until explicitly changed.

Integration policy (recommended):
- Auto-exit Sleep (sleep → occupied) before HA-driven wake commands so backend state matches user-visible behavior.
- Optionally (disabled by default): treat long-running Sleep sessions as “home/occupied” in the UI and perform a best-effort one-off backend cleanup write, **only once the backend reports `power=0`**.

---

## 11) Device State — Fields of Interest (Non-PII)

Commonly used fields:
- Identity/state: `id`, `name`, `online`, `power`, `mode`, `scenary`
- Mode capabilities: `modes` (bitmask), `availables_speeds`
- Setpoints: `cold_consign`, `heat_consign`
- Fan: `cold_speed`, `heat_speed`
- Timers/limits: `sleep_time`, `min_temp_unoccupied`, `max_temp_unoccupied`
- Optional constraints (when present): `min_limit_cold`, `max_limit_cold`, `min_limit_heat`, `max_limit_heat`

---

## 12) Open Questions / Next Validation

- Confirm behavior of P2=4 (HEAT_COOL/AUTO) across more installations before enabling by default.
- Confirm exact allowed ranges/encodings for slats (P9/P10/P19/P20) on at least one model.
- Confirm whether devices that do not support Auto fan (0) fall back to a manual speed (observed fallback: 1) rather than persisting 0 or returning an error.
