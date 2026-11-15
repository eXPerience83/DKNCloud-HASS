# DKN protected LAB — bounded setpoint trial implementation

**Status:** development-only safety foundation; **not a runnable real session**.
This document supplements `bounded-setpoint-trial-proposal.md` and issue #197.
The existing `REAL_DESTINATIONS_ENABLED = False`, `--protected-real` denial,
`ReadOnlyPolicy`, credentials and all production integration files are unchanged.

## Present components

- `scripts/setpoint_trial.py`: strict frozen plan from raw ON/occupied/COOL or
  HEAT telemetry, actual per-mode backend limits and a whole-degree setpoint;
  a single exact +1 °C change (or -1 °C at the upper bound) and original restore.
  `BoundedSetpointPolicy` permits only the two canonical P7/P8 envelopes when
  individually armed, checks the live raw device snapshot at transport time,
  and revokes restoration after a non-2xx, timeout or cancellation.
- `SingleUseTrialJournal`: a private, fsynced, exclusive persistent nonce claim
  plus irreversible per-wire-attempt records (no token or credential values).
  Its storage must remain outside the disposable HA configuration for any
  future real run. The file is never deleted to refund a failed session.
- `scripts/protected_lab.py` and `scripts/lab_policy.py`: narrow optional
  hooks to freeze control payloads before awaited traffic budgets, revalidate
  and persist the attempt at the actual delegate-to-aiohttp boundary; an
  optional monotonic recorder labels INFO separately from P7/P8 on `/events`.
  All default read-only policy behavior remains unchanged.
- `scripts/trial_timeline.py`: memory-only allowlisted evidence primitives,
  retaining relative order and provenance without account or device data.
- `scripts/lab_tls_backend.py`: the existing loopback TLS synthetic server
  still rejects writes by default. A test-only opt-in supports exactly the
  24 → 25 → 24 °C P7 sequence and emits synthetic device frames. It proves
  local code behavior, **not** actual vendor push causality.
- `protected_ha_lab` supports a synthetic-only `setpoint_trial` scenario
  driven through the genuine Home Assistant Climate service. It waits for
  Pusher-applied setpoint evidence, observes two ordinary INFO cycles and
  attempts a strictly admitted restore. Its subprocess exit gate requires
  two exact loopback transport controls and clean teardown.

## Already covered by offline regression tests

Plan bounds, OFF/Away/Sleep/alias rejection, absent/invalid backend limits,
exclusive journal claim and permissions, wrong P-code/target denial, duplicate
and concurrent attempts, payload mutation during budget waits, rechecking
raw device state, timeout/cancellation/429 revocation, and the two exact
synthetic HTTP control attempts via the verified loopback TLS gateway.
Unsolicited real vendor events have not been observed by this change.

## Remaining gates before live testing

1. Explicit owner-reviewed **per-session source digest, nonce, device/install
   scope and exact numeric plan**, validated before credential access, with
   a private durable journal surviving crash and restart.
2. A distinct, fail-closed real session entry point using the existing isolated
   HA process, protected transport and bounded supervisor. Do not modify the
   old read-only `real_policy()` or flip the global disabled flag; do not
   introduce a general-purpose writable LAB.
3. Real-session authentication and scoped subscription, raw
   Home/ON/COOL-or-HEAT and installation membership, no pending
   recovery/unknown state, explicit owner numeric-plan confirmation and
   exact reviewed full HA service → Climate → API → guarded transport
   acceptance. The synthetic service path is now exercised; it does not
   grant access to the real DKN server.
4. Real Pusher `cold_consign`/`heat_consign` field capture before merge,
   ordinary INFO dispatch/acceptance, GET and HA optimistic/confirmed
   publication with the same monotonic clock. No extra INFO/GET injected into
   the comparison window; observe at least two ordinary INFO cycles.
5. Synthetic tests of 429 retry from the authentic API, simultaneous service
   requests, pending reads, inconsistent Pusher, disconnects, crash/restart,
   no alternative clients/transports, cancellation/cleanup and no durable
   credential artifacts. Test final reviewed exact head against current and
   minimum pinned Home Assistant.
6. Only then obtain a **fresh, single-use and exact-plan owner authorization**
   and run the physical ±1 °C experiment. A restore is not guaranteed after an
   ambiguous result; stop and request owner intervention rather than replay.

PR #222 is independent; a P7/P8 trial cannot establish a scene-PUT guarantee.
Nothing in this draft permits a real device command.
