# Local secrets

This folder is for local/manual testing only.

Tracked files:
- `.gitignore`
- `dkn.env.example`
- `README.md`

Local-only files:
- `dkn.env`
- `*.env`
- `*.local`
- `*.json`
- `*.log`
- `manual-evidence/`
- `manual-runs/`

Never commit real Airzone credentials, tokens, full API URLs, request logs, or raw payload captures containing credentials.

For local testing:

```bash
cp secrets/dkn.env.example secrets/dkn.env
```

Use `secrets/dkn.env` for local backend evidence work. The real credential file
must stay local and ignored by Git.
