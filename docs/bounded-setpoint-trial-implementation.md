# Supervised DKN LAB implementation (#197 / #223)

The existing independent HA/TLS LAB now has an explicit session launcher.
The immediate experiment is **PUT → Pusher without a subsequent LAB INFO/GET**.
P7/P8 foundation tests remain synthetic; their results do not establish scene-PUT
behavior. No real session or physical acceptance is implied by synthetic success.
`REAL_DESTINATIONS_ENABLED = False` and the old `--protected-real` denial remain.
Production integration files and the default read-only policy are unchanged.

## Run locally without credentials

Use the locked Python environment from `AGENTS.md`:

```sh
uv run --locked --no-sync python scripts/run_supervised_ha.py
uv run --locked --no-sync python scripts/run_supervised_ha.py --mode synthetic-put-after-info
uv run --locked --no-sync python scripts/run_supervised_ha.py --mode synthetic-put --field min_temp_unoccupied
```

These launch actual independent Home Assistant processes with authentic Climate
or Number services and API code, scoped PUT admission, verified loopback TLS,
Pusher, normal INFO scheduling and GET readback. The backend has to opt in to
PUTs; by default it still rejects every control. Automatic numeric-plan approval
exists only when this loopback backend is present, never in a real session.

The original P7 case remains available with `scripts/run_isolated_ha.py
--protected --protected-case setpoint_trial`. `--mode synthetic-observe` exercises
the new read-only session entry point against loopback.

## Real preparation: two separate approvals

1. Obtain a fresh single-session approval for the exact source digest, private
   device/installation scope and selected `observe` or `put` mode. The launcher
   checks it **before reading stdin credentials** and the HA child checks it
   again. Get the reviewed digest without connecting:

   ```sh
   uv run --locked --no-sync python scripts/run_supervised_ha.py --digest
   ```

2. For `put`, after authentication, subscription ACK, valid prior device events
   and resynchronization, the LAB writes a private `plan.json`. Present the exact
   device, field, original, proposed value and effects to the owner. Only the
   matching private `approval.json` with `approved: true` arms the pair of PUTs.
   Approval of the session alone never permits a physical control.

A private 0600 grant has exactly these fields (actual identities stay private):

```json
{
  "approved": true,
  "mode": "put",
  "field": "scenary",
  "nonce": "fresh-random-nonce-of-at-least-32-characters",
  "source_digest": "reviewed-digest",
  "expires_at": "short-lived-UTC-ISO-8601-expiry",
  "device_id": "privately-selected-device",
  "installation_id": "privately-selected-installation"
}
```

Only `scenary` or `min_temp_unoccupied` is allowed in a PUT grant. For `observe`,
omit `field`; IDs may both be null to authorize discovery of an unambiguous
single-device account. More than one installation/device aborts rather than
expanding scope. Observation writes private `scope.json` and `put-plans.json`;
these are candidates, not control approvals. Never copy an observation nonce or
approval into a later PUT session.

Create a private 0700 persistent session directory outside the repository and
outside disposable HA configuration. Supply credentials as a JSON object with
`email` and `password` through a private pipe to stdin, then run:

```sh
# A private credential provider writes to the pipe; never print its output.
credential-provider | uv run --locked --no-sync python scripts/run_supervised_ha.py --mode observe --grant "$PRIVATE/grant.json" --session-dir "$PRIVATE/sessions"
```

Use `--mode put` for a separately approved PUT session. There is deliberately no
credential in argv, inherited child credentials in env, or generic write CLI.
The existing container's privately stored account may be used by its owner via
such a provider; this code does not search for or copy private credentials.
The launcher does not start unapproved live connections. Approvals are trusted
local owner inputs, not a sandbox against malicious code running as that owner.

Each session claims a durable nonce directory before reading credentials and
fsyncs a second exclusive child claim before the HA child reads the pipe.
Crashes, cancellation and failed login consume permission. Never delete or reset
these journals to repeat a session. Source digest includes runtime/harness files,
manifest/translations and lock contract. The integration copy is byte-checked.

## PUT scope and possible effects

- `scenary`: only raw and effective Home, powered ON, an ordinary supported mode,
  then exact `occupied → vacant → occupied`, using nested device payloads.
  Away can apply configured unoccupied limits and change demand or power. The
  owner must approve those effects. If this is unsuitable, do not run the scene
  experiment. No Sleep scene or P1/P2 command is permitted.
- `min_temp_unoccupied`: Home remains selected; integer values must be within
  the documented 12..22 °C range in `info.md`. Select +1 °C if valid, otherwise
  -1 °C, and restore exactly. This changes the Away heating minimum; it does not
  prove anything about delivery of a scene PUT.

All other PUTs, other devices, unexpected state changes, extra body fields,
P7/P8 and other P-channel controls are denied in a PUT session. Each actual
attempt is fsynced before delegation; no refunds or automatic retries. Authentic
API 429, timeout and cancellation regressions assert a single transport attempt
and revocation of restoration. An error/ambiguous result stops the experiment;
ask the owner to verify and decide how to restore, rather than replaying it.
Private claim/attempt/plan data survives for that intervention.

## Read isolation and evidence

The LAB-only `ReadWindow` drains already-started HTTP reads, then defers new
INFO/GET transport contexts. It neither closes Pusher nor changes production
SyncManager/scheduler code. This also holds post-PUT refresh and watchdog GETs
so they cannot contaminate the isolated window. Deferred operations resume
through the same ordinary request budget; no extra INFO is injected.

After approval, check enough time remains for restoration. Pause reads, log the
window start, invoke one authentic service PUT, and observe for **20 seconds**
following its HTTP result in real mode (**5 seconds** in synthetic fixtures).
Resume reads, observe two ordinary INFO cycles, perform a post-window GET,
then restore only after received telemetry corroborates the modified state.
A final later GET must corroborate the original value. No elapsed-time condition
or shutdown handler sends an automatic restoration PUT.

The same monotonic clock records last INFO before PUT, control invocation and
transport admission, HTTP status/result, actual decoded Pusher field/value before
merge, applied GET values, coordinator completion, optimistic overlay calls and
HA state publication. Transport admission precedes aiohttp I/O; it is not a
packet capture or an exact server timestamp. Pusher can arrive before HTTP ACK.
A public report contains only allowlisted status/timing/field/value data and
anonymized payload shapes, never authenticated URLs, channel names or IDs.

`put_push_during_isolation` distinguishes spontaneous delivery in this LAB's
window; `put_push_after_resume` counts only events after resumption. A result
before this LAB's next INFO does not exclude another app/HA client's reads.
After-INFO delivery is correlation, not independent proof of causality. GETs
can contain stale backend state; readback is not physical verification.

## Bounds, cleanup and acceptance

Real sessions have at most 285 s active / 300 s parent including cleanup,
40 total state attempts, two combined authentication attempts, one socket and
no reconnect. State starts remain spaced by at least 10 s. Approval waiting,
initial resync and read draining count toward these limits. Insufficient remaining
time aborts before control. Normal server trust/hostname verification, TLS >=1.2,
service-only compatibility and redirect rejection remain enforced; the existing
static-RSA compatibility suite lacks forward secrecy as documented in `info.md`.

Owner tasks and API operations are cancelled/joined before transport closure.
The parent kills/joins an over-deadline child and removes temporary configuration.
HA credential storage/logging are suppressed. Pending HTTP, storage, integration
byte identity and process cleanup are included in acceptance results.

Tests cover authorization/digest/expiry/scope, parent/child nonce reuse, exact plan
approval, two PUT attempts, read draining, default write denial and authentic API
429/timeout/cancellation without replay. Independent HA cases cover spontaneous
scene push, scene push deferred to INFO and simple-field push, restoration and
readback. Keep both pinned HA suites and legacy isolated setpoint acceptance.
Real outcomes and #222's ambiguous scene recovery remain separately assessed;
no intentional physical timeout, merge, release or production HA deployment is
part of this work.
