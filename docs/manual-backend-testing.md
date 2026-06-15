# Manual Backend Testing

These tools are for local DKN/Airzone Cloud backend audits only. They are not
part of the Home Assistant integration runtime, and CI must never call the real
backend or depend on local secret files.

## Local Credentials

Create a local `secrets/dkn.env` from `secrets/dkn.env.example`:

```bash
cp secrets/dkn.env.example secrets/dkn.env
```

```env
AIRZONE_USERNAME=your-email@example.com
AIRZONE_PASSWORD=your-password
AIRZONE_INSTALLATION_ID=
AIRZONE_DEVICE_ID=
```

`AIRZONE_INSTALLATION_ID` and `AIRZONE_DEVICE_ID` are optional. If they are not
set, the probe uses the first installation relation and first device returned by
the backend.

`secrets/.env` is still supported as a legacy fallback when `secrets/dkn.env` is
not present.

Never commit `secrets/dkn.env`, real logs, ZIP evidence, raw backend responses,
or anything under `secrets/manual-evidence` or `secrets/manual-runs`.

## Probe Commands

Preview a sanitized plan without credentials or network calls:

```bash
python scripts/manual_backend_probe.py --dry-run --safe-suite
```

List installations and devices with sanitized output:

```bash
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --list
```

Fetch a device snapshot:

```bash
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --snapshot
```

Run the conservative command suite:

```bash
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --safe-suite
```

Run one command:

```bash
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --command mode_cool
```

You can also point the probe at a credential file with `DKN_ENV_FILE`:

```bash
DKN_ENV_FILE=secrets/dkn.env python scripts/manual_backend_probe.py --snapshot
```

The probe writes sanitized artifacts to `secrets/manual-runs/<timestamp>/`.
It does not save raw login bodies, raw login responses, cookies, full sensitive
URLs, tokens, emails, device IDs, installation IDs, MACs, PINs, names, or
locations.

For write commands, the probe takes an initial `/devices` snapshot, sends the
canonical backend payload, verifies with another `/devices` snapshot, and tries
to restore the original field when the command changes state. A 2xx response
without a matching observed snapshot value is reported as
`accepted_but_not_verified`, not as a confirmed success.

## Dangerous Probes

Dangerous or experimental commands are skipped unless explicitly enabled:

```bash
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --command mode_heatcool_p2_4 --test-p2-4
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --command mode_alias_p2_8 --test-p2-8
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --command cool_auto_fan_p3_0 --test-auto-fan
python scripts/manual_backend_probe.py --env-file secrets/dkn.env --command cool_setpoint_out_of_range --test-out-of-range
```

`--dangerous-probes` enables all dangerous categories at once. Use it only for
local evidence gathering on a device you can safely recover.

Risks:

- `P2=4` is HEAT_COOL/AUTO and should only be used when supported by the
  `modes` bitmask or when intentionally forced for evidence.
- `P2=8` is an observed FAN_ONLY alias and may confuse the official UI until a
  normal supported mode is written again.
- `P3/P4=0` auto fan support depends on firmware and device behavior.
- Out-of-range setpoints are evidence-only probes and may be accepted by the
  backend even when the UI would reject them.

## Sanitizing Historical Evidence

Sanitize an old folder:

```bash
python scripts/sanitize_backend_artifacts.py secrets/manual-evidence --output sanitized --overwrite
```

Sanitize an old ZIP:

```bash
python scripts/sanitize_backend_artifacts.py secrets/manual-evidence/DKNCloud-tests-20260103-092223.zip --output sanitized --overwrite
```

The sanitizer creates:

- `SUMMARY.sanitized.txt`
- `commands.jsonl`
- `snapshots.jsonl`
- `findings.md`
- `redaction_report.json`

Review those files before sharing. Issues and PRs should include only sanitized
summaries, sanitized command records, and sanitized findings. Do not attach raw
PowerShell output, raw request files, raw response bodies, cookies, real IDs, or
ZIPs containing real backend evidence.
