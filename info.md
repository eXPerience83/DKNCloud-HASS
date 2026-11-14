# DKN Cloud for Home Assistant — Technical Reference

> **Scope:** Single source of truth for the DKN (Airzone Cloud) backend behavior that the Home Assistant integration relies on.  
> **Scope of evidence:** Documented request/response formats and device behavior; applicability may vary with the installation and firmware.  
> **Goal:** Clearly distinguish API responses, client-facing constraints, and conservative Home Assistant policies.

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
- `User-Agent: <browser-like UA>` (optional; keep HTTP client headers consistent)

---

## 2) Core Resources and Endpoints

### Login
- `POST /users/sign_in`
- Body: `{"email":"...","password":"..."}`
- **Success codes observed:** 200, 201

**Home Assistant integration behavior (not a backend contract):** Reauthentication
uses the existing config-entry email, strips legacy whitespace for both login
and authenticated runtime calls, and saves only the new token in entry options.
The password is never persisted. Missing identity aborts reauthentication
without contacting DKN; invalid credentials, timeouts and connectivity failures
remain distinct. A successful login is followed by a bounded authenticated
`GET /installation_relations` before the new token is saved: an HTTP 401 is
rejected, a transient failure leaves the old token untouched, and an empty
valid installation list is allowed. An HTTP 401 on authenticated reads raises
`ConfigEntryAuthFailed` for native Home Assistant reauthentication. For
authenticated device writes (`POST /events` and `PUT /devices/{id}`), a 401
also starts native reauthentication for the specific entry, since HA does not
automatically launch reauth from an entity service error. An authenticated 401
suspends automatic synchronization until entry reload. On success, Home
Assistant reloads the entry and constructs a fresh API client with the new
token. If the entry was never fully loaded or DKN returns the same token,
the flow explicitly schedules a reload; otherwise the update listener does so.
An entry-lifetime flag coalesces write-side reauthentication requests; reload
constructs a new client and resets that flag.
Propagated HTTP errors strip credentials and device identifiers from request
URLs, including hidden exception context, before reaching HA consumers.

### Integration safeguards (not a backend contract)

Installation validation accepts a bare list or an `installation_relations` list
whose elements are objects (dictionaries). Non-list payloads or lists with
non-object elements are invalid responses, not proof of token validity;
reauthentication preserves the existing token and reports cloud unavailability.
An empty list remains valid. During normal coordinator refresh an invalid
installation response raises `UpdateFailed` rather than treating the account as
empty and discarding the last good device snapshot.

Device snapshots accept a bare list or a `devices` list of objects. A valid
`[]` means no devices, but malformed HTTP 200 responses (including missing or
null `devices`) are treated as installation refresh failures. The coordinator
preserves the previous devices for failed installations while accepting healthy
updates from other installations; an invalid first snapshot fails setup.

Transport timeouts are re-raised as a generic `TimeoutError` outside the raw
exception handler. aiohttp connection timeouts can contain authenticated URLs;
the sanitized error retains timeout retry semantics without the raw error chain.
An interrupted JSON body upload can similarly produce a URL-bearing
`ClientOSError`. The integration propagates a sanitized transport exception
of the same type, without its original message or exception chain and without
automatically retrying a potentially submitted device write.

Reauthentication delegates reload only to the registered integration update
listener. Unrelated listeners do not suppress the explicit reload fallback.

Write-side reauthentication callbacks are cleared on entry unload and only
start flows for loaded or failed-unload entries while Home Assistant is not
stopping.
Migration logs do not include normalized account email addresses; persisted
config-entry identifiers remain unchanged.

### Sleep session cleanup and one-shot re-arm (draft #190)

The backend can leave raw `scenary=sleep` after a natural OFF. Two supervised
real five-minute Sleep cycles independently confirmed that guarded cleanup to
`occupied` allowed the following Sleep timer to run and end normally, with
no intervening manual Home command. The owner's final physical OFF observation
corroborated the cloud/HA state.

Automatic cleanup is **enabled by default** and can be disabled by the
`sleep_cleanup_enabled` option. A session-consistent ON followed by two
independent applied OFF reports authorizes one serialized, non-retried
`occupied` write. The first OFF requests bounded readback. No elapsed-time
condition authorizes an automatic scene or power write.

If automatic cleanup is disabled and the backend remains in raw Sleep while
reported OFF, an **explicit** new Sleep selection uses `occupied → sleep`
to rearm the one-shot scene. Both commands have automatic retries disabled.
An ambiguous or failed second command cannot be assumed to have restarted the
timer; recovery readback is requested. Explicit Home and HA wake commands can
also exit raw Sleep. Setpoint, fan and supported mode changes during active
Sleep preserve the scene.

The old `sleep_timeout_enabled` local effective-Home display was retired.
The shared five-minute margin is used **only** for the read-only ON-overdue
diagnostic. A reliable local session start and two fresh ON reports after the
deadline are required, and no power/scene action follows from the diagnostic.
Diagnostics exports include only aggregated anomaly counts, current count,
and the last detection timestamp, **without device IDs**; these counters are
kept since the current HA integration startup, not across HA restarts.
A timer edit or unknown session start invalidates the deadline inference.
Remote reports can be stale even when retrieved through a later GET.

Manual early OFF and an ON that persists beyond the deadline were not
provoked on real equipment; both have synthetic coverage. Live corrective
actions on an overdue-ON anomaly are intentionally deferred until observed
physical evidence supports a safe policy.

### Automatic synchronization policy (not a backend contract)

Push uses the service-specific secure WebSocket origin with certificate
and hostname verification enabled. The socket transport rejects redirects using
an isolated session and Home Assistant's verified TLS trust; ordinary REST
sessions are unchanged. The separately protected research laboratory still
disables live destinations by default.

Following explicit owner approval on 2026-11-12, WebSocket-only static-RSA TLS
compatibility is **enabled** for `wss://dkn.airzonecloud.com:8080/app/...`.
The service's official web client was independently observed negotiating
TLS 1.2 with `TLS_RSA_WITH_AES_128_GCM_SHA256`. The compatibility context adds
only OpenSSL `AES128-GCM-SHA256`, after modern suites, to a fresh verified HA
trust context for that origin. TLS 1.2 or newer, certificate and hostname
verification remain mandatory; redirects are rejected. REST and shared TLS
contexts are unchanged. Static RSA encrypts traffic but lacks forward secrecy.
There is no automatic cipher fallback or user-facing configuration switch.
Do not pin the service certificate fingerprint; normal certificate renewal
must continue to pass standard trust and hostname verification.
Diagnostics expose whether that policy is enabled. Pending channel authorization
is cancelled and awaited before closing the WebSocket transport, including when
connection establishment or entry shutdown fails.

The coordinator initializes from a device snapshot, then accepts partial push
updates without erasing absent fields. Each device requests information at a
nominal fixed ten-second cadence while push is active. During push outages,
automatic information requests and recovery snapshots share an entry-wide
budget of at least ten seconds between starts. A single-device fallback
alternates an information request and a snapshot ten seconds later; multiple
devices share installation snapshots and may have longer cycles. Spacing is
conservative from operation completion, so slow responses or server-directed
cooldown cannot release queued bursts. These timings
are client policies and do not guarantee physical-state convergence. REST
failures back off; healthy fallback does not back off solely for a push outage.
Failed connection attempts without any confirmed subscription do not invalidate
in-flight REST snapshots. A subscribed connection ending still fences old push
events and related in-flight reads.
A successful subscription requires resynchronization before normal operation.
Local time-based connectivity and scene maintenance continues without I/O.


Automatic recovery retains snapshot eligibility after transient information-request
failures. Information and recovery retries have separate deadlines; successful
snapshots do not confirm physical freshness or clear information failures.
If a climate mode change from OFF accepts the P1 power-on command but its
subsequent P2 mode command fails or is cancelled, the integration retains the
optimistic ON indication and requests a recovery readback. It propagates the
original failure instead of replaying an ambiguously accepted mode command.
This is a client recovery policy, not proof of physical activation.
Entry shutdown fences and awaits automatic scene cleanup. Channel authorization
runs separately from socket reception with per-channel retry backoff capped at
five minutes (the number of retries is not bounded).

Authenticated operations are fenced again after cooldown waits; entry suspension
cancels pending operations. Automatic scene cleanup shares device write locks.
Channel authorization failures retry independently without closing healthy channels.
A socket that stays active without acknowledging a channel subscription triggers
a bounded per-channel ACK timeout (60 seconds), followed by the same backoff;
other subscribed channels remain active. An ACK confirms subscription, not data
freshness or a completed device update.
The automatic request budget enforces spacing only; operation backoff and the
server-directed cooldown remain separate. Device control requests fail fast when
the remaining server cooldown exceeds ten seconds; long Retry-After responses
do not schedule delayed control retries. Diagnostics export health summaries
without raw device data or entry options.

Failed socket attempts do not trigger extra topology discovery. Reconnection
discovery waits for successful device resynchronization. Rejected information
requests also retain recovery eligibility; authentication and server-directed
rate-limit pauses remain authoritative.


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
- **Payload shapes used by this integration:**
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

> Alternative payload shapes may work in some environments. Use the documented shapes above for consistent integration behavior.

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

**Home Assistant switch delegation (integration policy, not a backend guarantee):**
Power switches prefer the sibling Climate entity's `turn_on`/`turn_off`
service while it has an active, usable HA state. If a Climate registry entry
remains but its HA state is missing or `unavailable` (for example, after
disabling the entity or when a restored state has no active Climate instance),
the switch uses its direct `P1` command fallback instead of assuming that a
successful service dispatch means a command reached the device. The fallback
uses the latest optimistic power state for idempotency. If a previous accepted
P1 is not yet reflected in an older backend snapshot, a new explicit opposite
power command is sent rather than discarded based on that cached snapshot.
The same intent rule applies to native Climate power services; an optimistic
state is a display/intent hint, not physical confirmation. Explicit Climate
and direct-switch P1 commands serialize their idempotency check, send and
optimistic update under the same per-device lock. A newer power request fences
an older request still waiting for the lock or preparing a Sleep/Away exit;
scene preparation releases the lock before retrying the guarded power send.
This prevents queued opposite intents from being dropped and duplicate
same-direction P1 sends; it does not imply physical confirmation.
Direct P1 fallback is allowed only if Climate is unavailable or its HA service
was never registered. After a dispatched Climate handler fails or times out,
its P1 outcome is unknown: the switch propagates the error and never replays
a second P1. This is a conservative client policy, not physical confirmation.
Delegated calls propagate the native HA entity context to preserve automation attribution.
Transport-level event payloads remain unchanged.


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

# 2) Device snapshot endpoint
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


### Sleep exit coordination (not a backend contract)

Automatic confirmed-OFF cleanup and active-control preparation serialize scene
exits with manual writes. They recheck current state and scene revisions after
acquiring the lock. Automatic Home writes are never replayed after ambiguous
results; readback requests are budgeted. HTTP acceptance alone does not prove
physical application. The older optional expired-Home UI view is not authority
for an automatic scene write.
Pending exit state is runtime-local. Restarted HA does not know when an already
active physical Sleep timer began; do not raise a deadline anomaly from that
assumption. The shared monitoring margin is **five minutes** for both INFO/Pusher
and GET paths; an ON beyond that deadline requests one recovery and requires two
post-deadline fresh ON reports before being shown as a *reported* diagnostic,
without sending an OFF or Home command. Automatic Sleep exit uses no API retry;
other manual commands follow the shared API policy: bounded short 429 retries,
never automatic replay after an ambiguous timeout or HTTP 5xx.

A non-Sleep backend observation retires a pending exit marker. A new explicit
Sleep selection re-arms local tracking; an older discarded cleanup must not
disarm that new selection. This does not establish physical timing.

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

## 4) HVAC Modes (P2) — Mapping, Classification, and Display Equivalences

### 4.1 P2 value → normalized display label

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
cold_modes = {1, 3, 4, 5, 6}  # COOL, VENTILATE, HEAT_COOL, DRY, cool-air
heat_modes = {2, 7, 8}  # HEAT, heat-air, (ventilate alias)
```

Practical guidance for the integration:
- **Fan speed writes:**  
  - If `mode ∈ cold_modes` → write via **P3** and read `cold_speed` (when fan control is applicable).  
  - If `mode ∈ heat_modes` → write via **P4** and read `heat_speed` (when fan control is applicable).
- **Applicability caveat:** membership in a routing set does not imply that **fan or setpoint** controls are available or appropriate in that mode:
  - **FAN_ONLY / VENTILATE (P2=3 or alias 8):** no setpoints; **fan control is exposed** (speed/auto depending on device support).
  - **DRY / DEHUMIDIFY (P2=5):** no setpoints; **fan control is typically unavailable**.
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

**Control-flow compatibility:** some clients change `"vacant"` to `"occupied"` before user-initiated actions (mode, fan, slats, etc.).  
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

- **Common client-facing range:** 16.0..32.0 °C.
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

- Some devices/firmware versions support a **real Auto fan** mode:
  - Write `"0"` to P3 (cool-type) or P4 (heat-type).
- Auto fan availability is device/firmware dependent.

**Non-contractual heuristic:** firmware identifiers are not reliable proof of Auto fan availability.  
The relevant device capability is whether it accepts/stores `"0"` for P3/P4.

**Future optional feature (integration):**
- A separate, opt-in *virtual* “Auto Fan” can be implemented for devices that **do not** support real Auto (heuristic control). This is **not** the same as device Auto and must be clearly labeled/opt-in.

---

## 9) Error Semantics Worth Handling

- `/events` may respond 2xx even if the physical unit later clamps/corrects the value.
- `/events` may return 422 when the WServer is not connected.
- `/events` may return 423 when the device/machine is not ready.
- Updates are asynchronous. HTTP success acknowledges backend receipt, not physical convergence; recovery snapshots may contain cached state.
- Automatic synchronization uses a fixed nominal cadence with recovery and backoff. There is no user-selectable refresh interval.
- Do **not** automatically replay an ambiguous scenary write (e.g., `sleep → occupied`): an accepted or timed-out response does not prove whether the backend applied it. Follow the per-device lock, session revision and read-back rules before considering another command.

---

## 10) Backend Robustness Expectations (Integration)

- Use the documented payload shapes (§2) for consistent integration writes.
- Treat **any 2xx** from `/events` as success; do not assume immediate state reflects the write.
- Prefer device snapshots (`GET /devices?...`) for verification and diagnostics.

### Sleep session behavior and cleanup (integration behavior)

Observed behavior:
- After `sleep_time` elapses, devices typically power off but may keep reporting `scenary="sleep"` until explicitly changed.

Integration policy (draft #190, two real natural Sleep cycles validated):
- Auto-exit Sleep (sleep → occupied) before HA-driven wake commands.
- When a current Sleep session has established an ON observation and two explicit OFF reports, request a single serialized `occupied` cleanup without waiting for timer expiry; verify backend readback before declaring convergence.
- Automatic cleanup is enabled by default and may be disabled with `sleep_cleanup_enabled`. The legacy timer-based Home display was removed. A stale or previous-session OFF never independently authorizes cleanup.
- If a session with sufficiently known start still reports ON after `sleep_time + 5 minutes`, request bounded recovery and expose a read-only `sleep_overdue` diagnostic only after repeated newly received ON; do not switch power or scene based on this diagnostic. Count the anomaly once per session in ID-free integration diagnostics for the current HA runtime.

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

### Snapshot ordering and control timeout limits

Local field revisions prevent a GET already in progress from replacing fields
received through push during that GET. They do not establish backend content
age: a later GET may still return older cached values, and same-connection
events have no verified monotonic version contract. No timestamp-based rejection
or arbitrary freshness window is imposed. Resolving contradictory observations
requires an explicit precedence contract; HTTP reception is not physical freshness.

Device-control transport timeouts and HTTP 5xx responses are never
automatically replayed: acceptance may have preceded either failure. This
applies to P1/P2 events and device-field PUTs, including scene changes.
HTTP 429 remains a server-directed rejection with bounded retry and cooldown;
long pauses still fail fast without scheduling a delayed command. Read-only
calls with `retry=True` retain their bounded timeout and 5xx retries.
INFO and automatic recovery/discovery GETs use `retry=False`; their schedulers
own backoff and do not perform an internal timeout retry. An ACK arriving during authorization or at its timeout
boundary prevents another subscribe or a false channel-disconnected notification.

A locally rejected Sleep exit carries an explicit no-send cooldown result. It
remains eligible after that monotonic deadline, without a recovery GET or rapid
retry loop. Any potentially submitted write retains its ambiguity guard. Entry
suspension takes precedence over cooldown rejection, including after waits.
