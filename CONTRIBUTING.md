# Contributing

- **Runtime compatibility:** Home Assistant currently runs on **Python >= 3.13.2**. Integration runtime code must remain compatible with this baseline (avoid Python 3.14-only syntax / stdlib features).
- **Tooling:** Local development tooling runs on **Python 3.14**.
- **Formatting & linting:** Format with Black (line length 88) and lint/sort imports with Ruff (`ruff check --fix --select I` followed by `ruff check`).
- **Translations:** Source of truth is `custom_components/airzoneclouddaikin/translations/en.json`. Keep every other locale file in sync with it.
- Do not add or rely on a `strings.json` file; translation updates should flow from `en.json` to the other language files.
- Preserve the coordinator-driven architecture and avoid introducing blocking I/O in the event loop.

## Commands

- `black --check .`
- `ruff check .`
- `pytest -q`
