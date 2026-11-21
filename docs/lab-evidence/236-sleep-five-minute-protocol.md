# Supervised DKN Sleep 5-minute experiments (supervised protocol)

Issue #236. Live execution requires the owner’s explicit approval of the reviewed source and a fresh private grant for one trial.

## Existing LAB boundary

The existing protected LAB explicitly blocks device control writes and terminates at 285/300 seconds. Trial A requires observation for at least 600 seconds after Sleep selection (plus orderly close). The expired, source-scoped grants from draft PR #223 are not reusable; #223 and #234 remain untouched. **Do not relax their limits or enable real destinations globally.**

The backend is known to leave raw scenary=sleep after a timed OFF. Two prior real 5-minute cycles established that this can happen, but they did not prove the timer behavior if Sleep is selected with power already OFF.

## Common preflight

- A single manually approved device and installation with operator physically present and able to turn it off locally.
- Confirm raw scenary=occupied; select and physically validate FAN_ONLY (P2=3) with fan speed 1 **before** the timed session. If unsupported or unconfirmed, stop. Do not change P2/P3/P4 during the measurement.
- Ensure that Home Assistant automations, its Sleep cleanup, mobile app clients and other controllers cannot issue competing commands. Any untracked write makes the outcome inconclusive.
- Confirm sleep_time=5 is accepted and read back as 5 minutes; the ordinary UI's range is larger, so do not assume every model accepts five minutes.
- Capture allowlisted REST fields and separately recorded physical state. This protocol opens no PUSH channel. Do not infer backend action solely from an HA optimistic overlay.

## Trial A: Sleep while OFF, ON at t=240 seconds

Time t=0 is the HTTP acceptance of scenary=sleep, not the beginning of the script.

| Offset | Expected supervised action |
|---|---|
| t=-90 | Baseline physically OFF, scenary occupied, FAN_ONLY low ready |
| t=-60 | PUT sleep_time=5, then verify readback |
| t=-15 | Repeat physical and raw backend baseline |
| t=0 | One PUT scenary=sleep; **do not send occupied during observation** |
| t=240 | One POST /events P1=1, verify that the fan actually turns on |
| t≈300 | Observe potential expiry 5 min from Sleep selection |
| t≈540 | Observe potential expiry 5 min from ON |
| t=600 | End observation and classify; cleanup only afterward |

Classify: **A0** Sleep cannot be armed while OFF / rejected, **A1** physical OFF near t≈300, **A2** OFF near t≈540, **A3** not observed by t=600, **INCONCLUSIVE** if no physical confirmation or competing writes/stale evidence. If A0 occurs, **do not send P1 as part of this experiment**.

Proposed per-session hard ceiling: 780 seconds including supervised close, subject to separate approval.

## Trial B: Sleep while ON, explicit OFF at t=120 seconds

| Offset | Expected supervised action |
|---|---|
| t=-90 | Baseline physically ON, scenary occupied, FAN_ONLY low |
| t=-60 | PUT sleep_time=5 and verify readback |
| t=-15 | Repeat physical and raw backend baseline |
| t=0 | One PUT scenary=sleep |
| t=120 | One POST /events P1=0. **Do not send occupied** |
| t≈150 | Confirm physical OFF and inspect raw scenary |
| t≈300 | Inspect whether raw scenary has become occupied naturally |
| t=390 | End observation and classify; cleanup only afterward |

Classify: **B1** raw backend occupied without any external scene write; **B2** raw backend remains sleep; **INCONCLUSIVE** if any third-party intervention, ambiguous result, or stale data.

Proposed hard ceiling: 570 seconds including supervised close, subject to separate approval.

## NAS launch after review

Use the checkout and pinned virtual environment in the existing Remote Dev
container. The existing Git-ignored `secrets/dkn.env` must be a regular file owned
by the operator with mode 0600. The runner reads the existing
`AIRZONE_USERNAME` and `AIRZONE_PASSWORD` directly into process memory, and
requires `AIRZONE_DEVICE_ID` and `AIRZONE_INSTALLATION_ID` in the same file.
If scope is absent, the owner must configure it locally before approval. There
is no real discovery run for obtaining scope. No shell sourcing, credential
arguments, credential copies, or subprocess environment variables are needed.

Preview the plans and obtain the reviewed source digest offline.

    .venv/bin/python -m scripts.sleep_lab_plan --scenario both
    .venv/bin/python -m scripts.sleep_lab_run --digest

After approval, the owner creates a private JSON grant (0600) outside tracked
paths. Supply exactly `approved` (true), `scenario`, `source_digest`,
`device_id`, `installation_id`, `nonce` (64 lowercase random hex characters)
and `expires_at` (timezone-aware ISO timestamp). Expiry must cover the full
session and be at most 30 minutes away. Create the fixed owner-only, persistent
`secrets/sleep-sessions` directory (0700) before the first launch. The grant is consumed before login.
An active lock left after a crash requires physical verification and owner
clearance before removal. Never delete a consumed claim to retry.

    .venv/bin/python -m scripts.sleep_lab_run --scenario off_sleep_then_on --grant "$grant_path"

For B use `on_sleep_then_off` and a new grant. Launch in the supervising
operator’s foreground terminal after isolating HA and all other writers.
Confirm the physical baseline and exclusivity by typing the scenario name.
Enter `ok`, `on`, or `off` at least every 30 seconds. Use `on` or `off`
when you physically observe a transition and at the end of the observation.
The runner cancels at 45 seconds without input or on EOF. Enter `stop` or
press Ctrl-C to end the session. SIGTERM also requests cancellation.

## Operation budget and evidence

The shared LAB gateway enforces a single TLS session with no HTTP redirects,
proxy environment, WebSocket, or alternate transport. The Sleep policy admits
only these operations on the DKN hostname.

| Requests | Operation |
|---|---|
| 1 login | POST /users/sign_in |
| 1 discovery read | GET /installation_relations. Require exactly the approved installation. |
| A 22 / B 15 device reads | GET /devices with the approved installation ID. Require exactly the approved device. |
| 1 write | PUT /devices/{approved device} with `{"sleep_time":5}`, then confirm by GET. |
| 1 write | PUT /devices/{approved device} with `{"device":{"scenary":"sleep"}}` at t0. |
| 1 write | POST /events with `cgi=modmaquina`, `option=P1`, the approved device, and string `value="1"` for A or `"0"` for B. |

The ceiling is one auth and 30 state requests in either trial, with three
ordered writes and at least 10 seconds between state requests. Preflight
steps run sequentially before t0. A lasts through t=600 with a 780-second
hard ceiling from session start. B lasts through t=390 with a 570-second
ceiling. Poll every 30 seconds after t0, omitting the poll in the P1 slot.
Readback follows HTTP acceptance and the normal spacing rule. Timing and
power-off classification must allow for polling and HTTP latency.

The private JSONL journal records operation attempts before submission, HTTP
status when available, monotonic offsets, allowlisted REST values and separate
physical entries. Map session offsets to t0 using the t0 record. The journal
excludes identifiers, URLs, tokens, credentials and raw payloads. There is
no PUSH evidence in this run. An attempt without a definitive outcome is
ambiguous and must never be replayed automatically.

Any HTTP failure, malformed required response, scope or baseline mismatch,
observer loss, cancellation or deadline ends the session. An unexpected
physical observation or suspected external writer requires the operator to
stop and mark the trial inconclusive. Treat a successful power HTTP response
as acknowledgment only. If neither physical observation nor readback shows
the intended transition, treat the evidence as inconclusive.

## Physical stop and restore

On failure or at the end of observation, verify physically and turn the unit
OFF using its local control if needed. After the journal closes, restore
`occupied` using separately approved manual control, confirm OFF and
`occupied`, then restore HA and other clients. A command already in flight
may still land after cancellation. Watch for delayed changes and record
unverified cleanup separately. Cleanup is outside the measured session and
its commands are excluded from the runner.

## Offline validation

    .venv/bin/python -m pytest -q tests/test_sleep_lab_plan.py tests/test_sleep_lab_run.py tests/test_lab_policy.py tests/test_protected_lab.py

The synthetic timeline tests exercise both schedules and stop paths without the
NAS file or real network. The existing gateway tests cover the shared
transport. Real operation remains pending owner approval. The result will
inform #234.
