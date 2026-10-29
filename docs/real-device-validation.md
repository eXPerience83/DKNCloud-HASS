# Real-device validation protocol (#109 phase 6)

This is a **manual gate**, never a CI or synthetic fixture result. Home Assistant
unit tests cannot prove DKN cloud-to-device state convergence. Do not merge
[#102](https://github.com/eXPerience83/DKNCloud-HASS/pull/102) on CI alone.

## Safe preparation

1. Prefer a separate Home Assistant test installation; otherwise back up your HA
   configuration, record the installed integration version, and plan rollback.
   Test only when changing HVAC state will not create discomfort or hazards.
2. Confirm the official app and physical controls work before testing HA. Record
   any independent cloud outage, rather than assuming an integration failure.
3. Write down the tested HA version and exact integration commit. Distinguish
   `main` from an explicitly installed **PR #102 test build**. The published
   0.5.0 alpha does not include the unmerged PR.
4. Refer to the target unit only as **Device A**. Never put real email, password,
   token, PIN, MAC, device/installation ID, coordinates or unredacted diagnostics
   into a public report. Keep PII entities disabled.
5. Use UTC timestamps and a stopwatch. For external state-change checks, attempt
   at least three trials, returning to a known state between them. Record fails
   and inconclusive cases instead of silently dropping them.

## Evidence capture, without credentials

`scripts/manual_device_evidence.py` is an offline, standard-library-only
observation recorder. It **does not** access the DKN backend, your HA instance,
the network, or raw log files. It only accepts predefined scenario, checkpoint,
source and result names, plus bounded numeric timing and safe HVAC fields.

Run from the repository root using Python 3.14 (or `py -3.14` on Windows):

```bash
python scripts/manual_device_evidence.py start --variant main --ha-version 2026.10.4
# Substitute the generated name below for RUN_NAME.
python scripts/manual_device_evidence.py record --run RUN_NAME --case official_app --checkpoint action --source official_app --result pass
python scripts/manual_device_evidence.py record --run RUN_NAME --case official_app --checkpoint snapshot_after --source backend_snapshot --result pass --elapsed-seconds 18 --mode cool --setpoint-c 24
python scripts/manual_device_evidence.py record --run RUN_NAME --case official_app --checkpoint ha_observed --source home_assistant --result pass --elapsed-seconds 29 --mode cool --setpoint-c 24
python scripts/manual_device_evidence.py finish --run RUN_NAME
```

For the PR test build set `--variant pr102`, ideally alongside
`--revision <full-40-character-sha>`. Run names are generated and contain no
hardware identifiers. Results are stored under the ignored
`secrets/manual-runs/RUN_NAME/` directory. The finalized `evidence.zip` contains
**only** `session.json`, `observations.jsonl` and `SUMMARY.md` built from the
allowlisted values, never the entire local directory. Review the ZIP before
publishing it. Each variant should have an independent run.

A `pass` label is a **manual assertion**: do not record it unless personally
observed. Use `pending`, `stale` or `unavailable` when appropriate. The elapsed
seconds field is stopwatch time since the associated action, not an HTTP
round-trip measurement. Do not attach raw request bodies, HAR, screenshots,
unredacted logs, tokens or HA diagnostics. The existing
[historical sanitizer](./manual-backend-testing.md) is available for old local
artifacts, but its outputs also require review.

The separate authenticated PowerShell HTTP harness remains proposed in
[#55](https://github.com/eXPerience83/DKNCloud-HASS/issues/55). This recorder
does not implement or claim to validate those direct backend probes.

## Test matrix

Use the matching `--case` value and checkpoint(s). For comparisons, record both
the observed state and whether it agrees with the expected value. When a
snapshot or request cannot be independently inspected, record it as unavailable
rather than claiming it happened.

| Case | Manual action and expected outcome | Timing/evidence |
| --- | --- | --- |
| `setup` | Install in test HA and add the account; no configuration errors | Entry created; no credentials in evidence |
| `registry` | Device A and expected climate, sensor, switch and number entities appear once | Confirm no duplicate or unexpectedly exposed PII entities |
| `state_read` | Compare current temperature, setpoint, power, mode and fan with the official app | Match/mismatch and safe mode, power, setpoint and fan |
| `ha_command` | From HA, change power, supported mode, target setpoint, supported fan | Confirm in app and physically; measure convergence; restore settings |
| `official_app` | From the official app, change power/mode/setpoint without HA interaction | Baseline and observations at about 0, 10, 30, 60, 120 seconds |
| `physical_controls` | Change a supported setting physically, without using the app/HA | Confirm when cloud snapshot and HA match; measure elapsed seconds |
| `refresh_102` | On an explicitly installed PR #102 build, track snapshot, refresh event, later snapshot, HA | Separate `snapshot_before`, `refresh_requested`, `snapshot_after`, `ha_observed` |
| `reload` | Reload integration in HA | Same devices/entities, no duplicates; updated state after settling |
| `restart` | Restart HA after backup | Entry and entities recover; no missing data after settling |
| `offline_recovery` | If safe, disconnect/reconnect device/network; observe cloud outages separately | Time to disconnected and recovered UI state |
| `options` | Change and restore polling/notification options | Config persists through reload; hidden token/options retained |
| `reauth` | In a planned maintenance window, test expiry/password change | Reauth must retain the same entry and existing options; #105 remains independently tracked |
| `marketplace_install` | On a tested HA 2026.11 build, install from native Marketplace | Pending until supported HA/PHACC and real Marketplace test |
| `hacs_upgrade` | Test HACS-to-Marketplace update in a backed-up instance | Pending until supported HA 2026.11 test |

Follow `info.md` for backend contracts. Do not force unsupported fan Auto,
P2 aliases or out-of-range setpoints. An HTTP success does not establish that
the machine applied a command.

## The #101/#102 refresh decision gate

1. Record the `main` baseline first; reproduce an external app/panel change,
   if possible. If main already converges, do not manufacture a failure.
2. Test the **exact PR #102 commit** in a separate backed-up HA test instance.
   Never assume a CI run or a release asset has deployed the PR.
3. Repeat the same app and physical-control changes at least three times.
   Observe the sequence: initial `GET /devices` snapshot; best-effort
   `cgi=infomaquina` **inside POST /events**; later `GET /devices` snapshot;
   finally the HA entity state. `infomaquina` is asynchronous, not a new HTTP
   endpoint or authoritative state response.
4. Mark the refresh request checkpoint as `pass` **only when dispatch itself is
   independently observed**. If only state convergence is visible, use
   `pending`/`unavailable` and describe that limitation in the PR report.
   Never publish raw authenticated URLs or response content to prove dispatch.
5. Check for request flooding, unnecessary delays, stale fallbacks, unexpected
   unload/reload behavior, and 401/reauth anomalies as far as safely possible.
6. Attach only the reviewed allowlisted ZIP to #101/#102 with the exact
   tested revision and observations. **Do not merge #102 without real-device
   evidence** showing improvement over the baseline and no regressions.

## Exit criteria

A merged docs/recorder PR makes the **protocol and safe evidence preparation**
available, but does **not** complete the physical-device validation. Keep
roadmap #109's hardware checklist and Marketplace #121's installation and
HA 2026.11 compatibility gates open until corresponding actual tests are run.
There is no release or minimum-HA change in this work.
