# Contributing

- The integration targets Python 3.14+, matching the Home Assistant 2026.3 runtime baseline.
  Use Python 3.14 for local development and CI parity.
- Format code with Black (line length 88, target-version `py314`) and sort/lint imports with Ruff. Use the exact versions pinned in `pyproject.toml` and `uv.lock`.
- The translation source of truth is `custom_components/airzoneclouddaikin/translations/en.json`. Keep every other locale file in
  sync with it.
- Do not add or rely on a `strings.json` file; translation updates should flow from `en.json` to the other language files.
- Do not introduce `%key:` translation references in this custom repository.
- Preserve the existing coordinator-driven architecture and avoid introducing blocking I/O in the event loop.
- The Home Assistant/PHACC/pytest test baseline is pinned together. Do not update
  pytest independently of PHACC; Renovate keeps PyPI updates behind a three-day
  maturity window and CI Python on the approved 3.14 patch series.
- Before submitting changes, run:
  - `uv lock --check` — verify the committed lockfile.
  - `uv sync --locked --only-group release` — install the locked test/lint environment.
  - `uv run --locked --no-sync ruff check .` — lint and import ordering.
  - `uv run --locked --no-sync black --check .` — formatting verification.
  - `uv run --locked --no-sync python -m pytest -q` — unit tests.
