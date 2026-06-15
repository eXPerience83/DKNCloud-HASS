# Manual Backend Evidence

These files and tools are for local DKN/Airzone Cloud evidence handling only.
CI must never call the real backend or depend on local secret files.

## Local Credentials

Create a local credential file from the versioned template:

```bash
cp secrets/dkn.env.example secrets/dkn.env
```

```env
AIRZONE_USERNAME=your-email@example.com
AIRZONE_PASSWORD=your-password
AIRZONE_INSTALLATION_ID=
AIRZONE_DEVICE_ID=
```

`secrets/dkn.env` is ignored by Git and must stay local. Do not commit real
credentials, tokens, full API URLs, raw request logs, ZIP evidence, or backend
payload captures containing private data.

## Evidence Folders

Historical backend evidence belongs under:

```text
secrets/manual-evidence/
```

Sanitized local outputs belong under:

```text
secrets/manual-runs/
```

Both folders are ignored by Git. Keep raw PowerShell output, raw request files,
raw response bodies, cookies, real IDs, MACs, PINs, names, locations, and ZIPs
with backend evidence out of commits.

## Sanitizing Historical Evidence

The historical PowerShell probe was the manually tested tool for real backend
writes. This PR does not add a Python write probe. A safer Python probe can be
introduced in a later PR after manual validation against the real backend.

Sanitize an old folder:

```bash
python scripts/sanitize_backend_artifacts.py secrets/manual-evidence --output sanitized --overwrite
```

Sanitize an old ZIP:

```bash
python scripts/sanitize_backend_artifacts.py secrets/manual-evidence/DKNCloud-tests-20260103-092223.zip --output sanitized --overwrite
```

The sanitizer reads ZIP entries in memory, ignores unsafe ZIP paths, and writes:

- `SUMMARY.sanitized.txt`
- `commands.jsonl`
- `snapshots.jsonl`
- `findings.md`
- `redaction_report.json`

Review sanitized outputs before sharing. Issues and PRs should include only
sanitized summaries, sanitized command records, and sanitized findings.
