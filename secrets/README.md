# Local secrets

This folder is for local/manual testing only.

Tracked files:
- `.gitignore`
- `.env.example`
- `README.md`

Local-only files:
- `.env`
- `*.env`
- `*.local`
- `*.json`
- `*.log`

Never commit real Airzone credentials, tokens, full API URLs, request logs, or raw payload captures containing credentials.

For local testing:

```bash
set -a
. secrets/.env
set +a
```

The real `secrets/.env` file must stay local and ignored by Git.
