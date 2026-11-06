# Contributing

- The integration targets Python 3.14+ and supports Home Assistant **2026.11.0 or newer**. The pinned test baseline is 2026.11.0. Use Python 3.14 for local development and CI parity.
- Format, lint, and sort imports with Ruff (line length 88, target-version `py314`). Use the exact version pinned in `pyproject.toml` and `uv.lock`.
- The translation source of truth is `custom_components/airzoneclouddaikin/translations/en.json`. Keep every other locale file in
  sync with it.
- Do not add or rely on a `strings.json` file; translation updates should flow from `en.json` to the other language files.
- Do not introduce `%key:` translation references in this custom repository.
- Preserve the existing coordinator-driven architecture and avoid introducing blocking I/O in the event loop.
- The official `pytest-homeassistant` harness replaces PHACC for HA 2026.11. Both the integration minimum and test baseline advance together; no 2026.10.4 compatibility lane remains. Renovate retains the three-day maturity window. See the [HA 2026.11 smoke checklist](./docs/ha-2026.11-smoke.md).
- Before submitting changes, run:
  - `uv lock --check` — verify the committed lockfile.
  - `uv sync --locked --only-group release` — install the locked test/lint environment.
  - `uv run --locked --no-sync ruff check .` — lint and import ordering.
  - `uv run --locked --no-sync ruff format --check .` — formatting verification.
  - `uv run --locked --no-sync python -m pytest -q` — unit tests.
