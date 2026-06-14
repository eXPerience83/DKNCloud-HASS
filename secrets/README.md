# Local testing secrets

This directory holds local-only credentials for manual testing of the DKN
Cloud integration against a real Airzone Cloud account.

## Files

| File            | Tracked | Purpose                       |
| --------------- | ------- | ----------------------------- |
| `.env.example`  | Yes     | Template with placeholder values |
| `.env`          | No      | Your real credentials (gitignored) |
| `.gitignore`    | Yes     | Prevents accidental commits of secrets |

## Setup

```bash
cp secrets/.env.example secrets/.env
# Edit secrets/.env with your real Airzone Cloud credentials
```

## Safety

- **Never commit** `.env`, `*.env`, `*.local`, `*.json`, or `*.log` files inside
  this directory.
- The root `.gitignore` and `secrets/.gitignore` both protect against accidental
  commits.
- CI **must never** depend on `secrets/.env` or real Airzone credentials. All
  CI tests use mocked API responses.

## Usage

Source the env file before running tests or tools locally:

```bash
export $(cat secrets/.env | xargs)
pytest -q
```

Or prefix individual commands:

```bash
env $(cat secrets/.env | xargs) pytest tests/test_airzone_api.py -q
```
