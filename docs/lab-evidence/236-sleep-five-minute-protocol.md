# Supervised DKN Sleep experiments

Issue #236. The authenticated discovery below used only login and GET.
A, C and F automate commands in that order. B follows in a separate invocation after review. Each trial requires a fresh grant and physical supervision.

## Discovery on the NAS

One installation and one device were returned. The sole device is proposed
for all three trials. Its snapshot reported OFF, `occupied`, mode 2,
`cold_speed=1`, three available fan speeds and `sleep_time=30`. The mode mask
advertised P2=3 and the connection timestamp met the integration's freshness
threshold. In P2=3 the integration uses P3 and `cold_speed`; low is code 1.
The snapshot confirms stored values and advertised capabilities. Physical
fan operation and acceptance of five minutes remain unverified for this unit.

The discovery command uses the Git-ignored, owner-only `secrets/dkn.env` and
the protected TLS gateway. It allows one login, one installation listing and
one GET for each returned installation, up to eight. It stores the scope in
`secrets/sleep-scope.json` (0600) only for an unambiguous single device.
Candidate labels are for the local terminal if multiple devices are returned.
A live trial also requires a singleton installation and singleton device
response matching its grant. Any changed or ambiguous scope stops the trial.

    .venv/bin/python -m scripts.sleep_lab_discovery

## Client, integration and backend evidence

- Android 1.9.0, local XAPK build 1090003, separates the
  `device.new_scenary` watcher (nested scene update) from the
  `device.new_power` watcher (P1 event) in
  `assets/www/multimarca/multimarca-controllers.js`.
  The scene watcher contains special handling for `vacant`. Its Sleep
  selection path does not dispatch a P1 ON. This is static client evidence.
  The 1.8.31 package was unavailable in the inspected reference paths.
- The HA Climate and Switch wake paths use the Sleep exit helper.
  Climate can also rearm a terminated Sleep scene. The coordinator may
  write `occupied` after corroborated OFF. These are potential interfering
  writes, so stop or isolate HA automations, HA integration and app writers
  for the whole measurement.
- Prior natural timeout observations in `info.md` report OFF with raw
  `sleep` retained. Discovery gathered a baseline only. None of these
  observations determines when the timer starts in A or how B and C differ.

## Preflight and authorized operations

Prepare the device manually outside the trial. Confirm physical baseline,
`occupied`, actual mode code P2=3 and low fan speed 1. A reported alias
P2=8 uses a different fan channel and stops the trial. Arrange reliable local
OFF and exclude other writers. The LAB writes `{"sleep_time":5}` once and
requires a fresh scoped GET returning exactly five before selecting Sleep.
No alternative duration is substituted. The duration write is part of each independently approved trial and occurs before t0. F uses one minute and stops unless that exact value is returned.

| Trial and scenario | Starting power | Duration | Intervention | Last observation | Hard ceiling |
|---|---|---:|---|---|---:|
| A `off_sleep_then_on` | OFF | 5 min | t=240 s, one P1 ON | t=600 s | 780 s |
| C `on_sleep_then_off` | ON | 5 min | t=120 s, one P1 OFF | t=390 s | 570 s |
| F `one_minute_sleep_rewake` | ON | 1 min | One P1 ON, 20 s after physical OFF confirmation | 90 s after physical ON confirmation | 360 s |
| B `on_sleep_then_panel_off` | ON | 5 min | t=120 s, physical panel OFF | t=390 s | 570 s |

t0 is the scene HTTP acknowledgment. Confirm fresh GET has the original
power and raw `sleep` before intervening. A requires physical ON corroboration,
then observation of any OFF or spontaneous scene change, especially near
t=300 and t=540. In B, turn OFF at the physical panel on the prompt and enter
`panel-off` within 15 seconds. This records operator confirmation time and
triggers a GET. The B policy contains no event write. In C, record physical
OFF separately after the P1 command. Compare the first GET showing
`occupied` and the last GET showing `sleep` against the recorded physical
transition. A reported scene does not establish the fate of any internal timer.

In F, enter `off` as soon as the first natural physical OFF is visible.
The LAB records that terminal receipt as the time origin and corroborates
OFF with a GET that also records the reported scene. If the confirmation is
missing at t=120, or GET does not corroborate OFF, stop without P1. Send a
single P1 ON 20 seconds after the terminal receipt. A late command slot
stops the trial. Enter `on` when the equipment physically restarts, within
20 seconds of the HTTP acknowledgment. Observe through 90 seconds after
that physical confirmation and use `off` again if it stops. No `occupied`
is sent by the LAB even if GET reports a spontaneous scene change. A second
OFF near 60 seconds is compatible with rearming only after corroboration
and exclusion of external writers. Remaining ON is compatible with a
one-shot or cancelled timer. Missing physical proof or delivery is inconclusive.

| Authorized request | A | C | F | B |
|---|---:|---:|---:|---:|
| POST `/users/sign_in` | 1 | 1 | 1 | 1 |
| GET `/installation_relations` | 1 | 1 | 1 | 1 |
| GET `/devices` with approved installation | 22 | 15 | 13 | 16 |
| PUT `/devices/{approved device}` with `sleep_time` | 1 (5 min) | 1 (5 min) | 1 (1 min) | 1 (5 min) |
| PUT same device with `{"device":{"scenary":"sleep"}}` | 1 | 1 | 1 | 1 |
| POST `/events`, `modmaquina`, `P1`, approved device | 1, string `"1"` | 1, string `"0"` | 1, string `"1"` | 0 |
| Total state requests | 26 | 19 | 17 | 19 |

The HTTPS origin is fixed. The transport blocks redirects, proxy environment
and alternate sessions. There is at least 10 seconds between state requests.
A, C and B poll every 30 seconds, reserving remote P1 slots. F uses a GET at the physical OFF confirmation and 10-second GET intervals after physical restart.
The middleware surfaces delivery failures before aiohttp can retry. No
`occupied`, info event, mode or fan write is in the measurement allowlist.
Passive Pusher requires an extra TLS socket and subscription authorization;
INFO additionally sends a POST. Both are outside these approved budgets.
This protocol gathers REST snapshots and physical observations only.

## Launch, supervise and restore

Use the existing virtual environment and an operator foreground terminal.

    .venv/bin/python -m scripts.sleep_lab_plan --scenario all
    .venv/bin/python -m scripts.sleep_lab_run --digest
    .venv/bin/python -m scripts.sleep_lab_run --automatic "$grant_a" "$grant_c" "$grant_f"

For B, after reviewing the three completed sessions, use its separate grant.

    .venv/bin/python -m scripts.sleep_lab_run --scenario on_sleep_then_panel_off --grant "$grant_b"

After review, supply a separate owner-only JSON grant (0600) for each trial.
The exact fields are `approved` (true), `scenario`, `source_digest`,
`device_id`, `installation_id`, `nonce` (64 random lowercase hex characters)
and `expires_at` (timezone-aware ISO timestamp). Copy only the private scope
values locally from `secrets/sleep-scope.json` when creating the grant.
Each grant must still cover its own hard ceiling when its session starts and expire within 30 minutes. The automatic sequence never renews or reuses grants. Create
`secrets/sleep-sessions` (0700) once. The runner durably consumes the grant
before login. A private campaign lock prevents another launch during preparation
or restoration. Each session closes its network before asking the owner to
corroborate the outcome and verify the restored state. If the outcome or
restoration is uncertain, stop. If an abort leaves an active lock, the owner
must verify OFF and raw occupied independently before clearing it. Only clear
a campaign lock after confirming its process has exited. Keep consumed claims.

After A, C and F have all completed and been restored, the runner writes a
private completion receipt for the current source and target. B requires
that receipt and the owner's interactive `reviewed` confirmation. B is
never part of `--automatic`.

Prepare mode, speed and initial power separately under their own authorization. Confirm the physical baseline and isolation by typing the scenario name.
Send `ok`, `on` or `off` every 30 seconds. Use the physical observation
keys on transitions and at the end. The runner stops after 45 seconds without
supervision, on EOF, `stop`, Ctrl-C, SIGTERM, scope errors, HTTP failures or
ambiguous delivery. Enter `interference` if another writer may have acted.
The runner stops and records that provenance.

After the observation closes or aborts, use the physical OFF control as
needed, wait for delayed changes, then restore `occupied` with separately
authorized manual control. Confirm physical OFF and raw `occupied` before typing `restored` or re-enabling other clients. The confirmation is an operator attestation based on that separate verification. Record `cleanup_unverified` if either is uncertain.
An in-flight request can land after cancellation. Never replay an ambiguous
request automatically.

## Evidence and interpretation

The private JSONL records monotonic offsets and UTC timestamps for attempts,
headers sent, HTTP results, GET snapshots, panel prompts and confirmation,
physical reports, interference and closure. Headers sent is a client-side
transport observation. HTTP acceptance and GET receipt are separate from
physical transition time. Retain only sanitized state and status fields.
Report timing as intervals where sampling or observer latency matters.

- **Demonstrated:** corroborated physical transition and its measured interval,
  or a fresh raw scene sequence with external writers excluded.
- **Compatible hypothesis:** A's verified OFF near 300 or 540 seconds is
  compatible with timing from scene selection or from power-on, respectively.
  Verified ON throughout the observation establishes only that no OFF was
  observed in that window. B/C scene changes can be compared by their intervals.
- **Inconclusive:** missing physical confirmation, stale or contradictory
  evidence, unconfirmed command delivery, or ambiguity about timer state.
- **Interference or failure:** record the source if known and exclude the trial
  from backend attribution. A closed observation is pending correlation with
  physical evidence.

## Optional follow-up designs

- D starts OFF in `occupied`, confirms FAN_ONLY low and five minutes, then
  selects Sleep at t0. Observe through t=600 s without any ON or P1. A retained
  scene while OFF cannot by itself establish whether an internal timer expired.
- E starts ON in the same baseline as B. Select Sleep at t0, use the panel
  for OFF at t=120 and ON at t=240, and record both physical confirmations.
  Observe through t=600 s to cover the original and possible restarted
  deadlines. No LAB P1 or scene restoration during observation.

D/E are planning designs only. Each needs a separate allowlist, approval, budget and synthetic validation if selected after the four trials.

## Offline checks

    .venv/bin/python -m pytest -q tests/test_sleep_lab_plan.py tests/test_sleep_lab_run.py tests/test_lab_policy.py tests/test_protected_lab.py

The test harness uses the offline schedule, local TLS, injected failures and a
synthetic operator input. No trial runs against the real account. The result
will inform #234 after collection and attribution of the physical evidence.
