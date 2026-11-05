# Development, testing and CI

This is maintainer-facing documentation. For installation and user features, start with the [README](../README.md). For backend commands and device behavior, use the [technical reference](../info.md).

## Supported baseline and compatibility policy

- **Minimum Home Assistant version:** 2026.10.4, as declared in `hacs.json`. Raising the minimum requires a separate, reviewed support-policy change.
- **Required locked test baseline:** Python **3.14.10**, Home Assistant **2026.10.4**, `pytest-homeassistant-custom-component==0.13.375` (PHACC). All exact development/test dependencies are recorded in `uv.lock`.
- **Next compatibility target:** Home Assistant **2026.11.0**, including the built-in Marketplace. The official `pytest-homeassistant` harness replaces PHACC upstream, but the 2026.11-compatible official stable package was blocked by an [upstream publishing issue](https://github.com/home-assistant/core/issues/190470) as checked on 2026-11-05. Verify upstream again before migrating: a passing 2026.10.4 suite or legacy PHACC canary does **not** validate 2026.11.
- **Python 3.15:** Not validated for the current Home Assistant/PHACC stack.

Work on the HA 2026.11 migration is tracked in [#109](https://github.com/eXPerience83/DKNCloud-HASS/issues/109), with the separate [2026.11 compatibility and real-device sign-off checklist](./ha-2026.11-smoke.md) tracked in [#121](https://github.com/eXPerience83/DKNCloud-HASS/issues/121). If the locked HA baseline advances without changing the minimum, restore an independently pinned minimum-version test lane first.

## Local checks

Install [uv](https://docs.astral.sh/uv/) and run:

```sh
uv lock --check
uv sync --locked --only-group release
uv run --locked --no-sync python -m pytest -q
uv run --locked --no-sync ruff check .
uv run --locked --no-sync ruff format --check .
```

The `test` dependency group contains the Home Assistant test harness; `lint` includes Ruff; `release` includes both. Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for translation rules and contribution guidance. Do not independently upgrade `pytest-asyncio` outside the HA/PHACC dependency pair.

## Coverage and DKN regression tests

The normal locked test workflow emits `coverage.json` and `coverage.xml` as GitHub Actions artifacts and a per-module coverage summary. The **incremental statement-coverage gates** are:

| Area | Minimum |
| --- | ---: |
| Overall | 74% |
| `climate.py` | 65% |
| `airzone_api.py` | 75% |
| Coordinator (`__init__.py`) | 73% |

To reproduce those gates locally:

```sh
uv lock --check
uv sync --locked --only-group test
uv run --locked --no-sync python -m pytest -q \
  --cov=custom_components.airzoneclouddaikin \
  --cov-report=term-missing:skip-covered \
  --cov-report=xml:coverage.xml \
  --cov-report=json:coverage.json
uv run --locked --no-sync python scripts/check_coverage.py \
  coverage.json --min-total 74.0 \
  --module-min __init__.py=73.0 \
  --module-min airzone_api.py=75.0 \
  --module-min climate.py=65.0
```

Tests use **synthetic, deterministic data**, the Home Assistant in-memory harness and local loopback HTTP. They cover setup, entity/device registries, unload/reload/restart simulation, legacy migration, concurrent installations, errors/retries and DKN climate commands (`P1/P3/P4/P7/P8`). They do **not** log into the real DKN service or certify a physical device. For real-hardware validation see the [device evidence protocol](./real-device-validation.md) and [HA 2026.11 smoke checklist](./ha-2026.11-smoke.md). Do not put account credentials, MACs or PINs into public CI artifacts.

## CI and dependency maintenance

- **Required CI:** Lint (locked Ruff); Tests (locked HA/PHACC and coverage gates); Package Test (integration-root ZIP layout). The release workflow performs its own full validation. Actionlint/ShellCheck, zizmor, pinned actions and least-privilege tokens are part of workflow hardening.
- **Daily compatibility canary** ([workflow](../.github/workflows/ha-compatibility-canary.yml), 04:47 UTC): Selects the latest compatible stable HA/PHACC pair available to the legacy harness and runs the full pytest suite in isolation. It reports lag and failures, but does not block PR CI, update the lockfile or validate the new HA 2026.11 official harness.
- **Daily baseline updater** ([workflow](../.github/workflows/ha-test-baseline-updater.yml), 06:43 UTC): Proposes an update only for a matched stable HA/PHACC pair after the **three-day maturity window**. It validates the proposed dependency delta and may open one **review-only PR**. It does not change `hacs.json`, the minimum-HA test lane or runtime code; no automerge or automatic release.
- **Renovate:** PyPI updates respect `exclude-newer = "3 days"`. HA and PHACC are managed together, outside ordinary Renovate dependency bumps. Changes to the test harness need independent review.
- **Package boundary:** Release ZIPs contain only `custom_components/airzoneclouddaikin`. The integration version is in its `manifest.json` and is **independent** of the development tooling version in `pyproject.toml`.

For preparing and publishing a version, use [RELEASING.md](../RELEASING.md). Releases are prepared as drafts and published manually; tests passing is not permission to claim hardware compatibility or to publish automatically.
