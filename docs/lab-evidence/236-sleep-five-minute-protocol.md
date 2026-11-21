# Supervised DKN Sleep 5-minute experiments (offline preparation)

**Issue:** #236. **Status:** OFFLINE PLANNER ONLY. No credentials are loaded and no HTTP requests or physical controls can be sent. Do not interpret this preparation as authorization for a live experiment.

## Why a separate LAB is necessary

The existing protected LAB explicitly blocks device control writes and terminates at 285/300 seconds. Trial A requires observation for at least 600 seconds after Sleep selection (plus orderly close). The expired, source-scoped grants from draft PR #223 are not reusable; #223 and #234 remain untouched. **Do not relax their limits or enable real destinations globally.**

The backend is known to leave raw scenary=sleep after a timed OFF. Two prior real 5-minute cycles established that this can happen, but they did not prove the timer behavior if Sleep is selected with power already OFF.

## Common preflight

- A single manually approved device and installation with operator physically present and able to turn it off locally.
- Confirm raw scenary=occupied; select and physically validate FAN_ONLY (P2=3) with fan speed 1 **before** the timed session. If unsupported or unconfirmed, stop. Do not change P2/P3/P4 during the measurement.
- Ensure that Home Assistant automations, its Sleep cleanup, mobile app clients and other controllers cannot issue competing commands. Any untracked write makes the outcome inconclusive.
- Confirm sleep_time=5 is accepted and read back as 5 minutes; the ordinary UI's range is larger, so do not assume every model accepts five minutes.
- Capture sanitized raw GET/PUSH fields and separately recorded physical state. Do not infer backend action solely from an HA optimistic overlay.

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

## Safety and evidence for a future real runner

The live runner is **NOT implemented in this PR**. Before one could be used, it must be independently reviewed and require a new human-approved, short-lived, **source- and device-bound grant for each trial**. Reuse the existing protected LAB's deny-by-default design, no uncontrolled network paths, exact operation allowlists, redirect prohibition, quotas, deadlines, cancellation/cleanup tests and read/write event separation; implement trial-specific limits without weakening the normal LAB.

Allowed during each measured phase: one sleep_time=5 PUT, one Sleep scene PUT, one approved P1 (ON for A, OFF for B), bounded raw GET/PUSH observations. Mode/fan setup and safe restoration require separately approved scopes. **Never automatically retry an ambiguous control write**. Stop on 401/429/5xx, unknown dispatch outcome, unsupported firmware, wrong initial state, loss of observer, unexpected scene/mode, competing writes, quota/deadline, or security gate failure.

After the observation closes, the operator confirms physical OFF and scene occupied, using an independently approved cleanup if necessary. An uncertain cleanup is never automatically replayed; record cleanup_unverified until physically corroborated.

Store only redacted timestamps, HTTP statuses and raw non-sensitive state fields. Distinguish dispatch vs acknowledgment, GET request vs receipt and physical vs PUSH vs REST observations.

## Offline preview and validation

Run from the repository root:

    python -m scripts.sleep_lab_plan --scenario both
    python -m pytest -q tests/test_sleep_lab_plan.py

The runner has no network imports, does not read credentials, and rejects any attempt to pass --real. A live run remains **blocked** until a separate guarded implementation, CI/security review and explicit device/command authorization.

**Decision gate:** collect evidence and revisit #234's Sleep policy only afterward. Keep #191/#121 release and physical acceptance separate.
