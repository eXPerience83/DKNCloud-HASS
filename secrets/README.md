# Local secrets

This folder is for local/manual testing only.

Tracked files:
- .gitignore
- .env.example
- README.md

Local-only files:
- .env
- *.env
- *.local
- *.json
- *.log

Never commit real Airzone credentials, tokens, full API URLs, request logs, or raw payload captures containing credentials.

## Usage

Source the env file before running tools:

```bash
set -a
. secrets/.env
set +a
pytest -q
```

One-liner for ad-hoc commands:

```bash
(set -a && . secrets/.env && pytest tests/test_airzone_api.py -q)
```
