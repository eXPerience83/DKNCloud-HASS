# Development, testing and CI

This is maintainer-facing documentation. For installation and user features, start with the [README](../README.md). For backend commands and device behavior, use the [technical reference](../info.md).

## Supported baseline and compatibility policy

- **Minimum Home Assistant version:** **2026.11.0** (`hacs.json`). This is a deliberate support-floor change: older Home Assistant versions are not supported by new builds.
- **Required locked test baseline:** Python **3.14.10**, Home Assistant **2026.11.0**, official `pytest-homeassistant==2026.11.0`. The reviewed `uv.lock` must match exactly. A separate, hash-locked minimum-HA lane is fixed to 2026.11.0 and always executes its full suite, even when the main baseline is also 2026.11.0, because transitive dependency graphs can differ.
- **Validation:** The official stable HA 2026.11 locked suite and the independent, SHA-256 hash-locked minimum compatibility suite passed on `main` on 2026-11-08 (including Ruff, HACS, hassfest and ZIP packaging). Real-device and native Marketplace smoke testing remain separate, pending gates (#121). The earlier 384-test beta probe (#164) was preliminary evidence only.
- **Python 3.15:** Not validated for the current HA 2026.11 harness.

The migration is tracked in [#109](https://github.com/eXPerience83/DKNCloud-HASS/issues/109) and [#163](https://github.com/eXPerience83/DKNCloud-HASS/issues/163); the independent [HA 2026.11 real-device checklist](./ha-2026.11-smoke.md) is tracked in [#121](https://github.com/eXPerience83/DKNCloud-HASS/issues/121).

## Test compatibility lanes

- **Minimum:** `.github/requirements/minimum-ha.in` and `minimum-ha.txt` pin HA and the official pytest harness at 2026.11.0, with hashes for the complete dependency graph. `.github/workflows/ha-minimum-compatibility.yml` installs and tests the independent locked environment on PRs, main, weekly schedule, and manual dispatch. Only a deliberate support-policy change may regenerate or raise the minimum pins.
- **Current:** `pyproject.toml` and `uv.lock` are the required release/test baseline and may advance independently, but never below the minimum. `tests.yml` checks matching official HA/harness versions and the minimum policy.
- **Latest stable canary:** runs daily at 04:47 UTC, selects a published stable HA plus official `pytest-homeassistant` pair from PyPI, installs the pinned auxiliary DKN test dependency (`aiointercept`) and runs the full suite. It reports upstream lag, does not change the repository, and does not validate real hardware.
- **Reviewed baseline updater:** runs daily at 06:43 UTC, waits at least three days for stable package maturity, validates the lock delta and full tests, and proposes a review-only PR. The independent minimum lane stays at 2026.11.0.

## Local checks

Install [uv](https://docs.astral.sh/uv/) and run:

```sh
uv lock --check
uv sync --locked --only-group release
uv run --locked --no-sync python -m pytest -q
uv run --locked --no-sync ruff check .
uv run --locked --no-sync ruff format --check .
```

The `test` dependency group contains the Home Assistant test harness; `lint` includes Ruff; `release` includes both. Follow [CONTRIBUTING.md](../CONTRIBUTING.md) for translation rules and contribution guidance. Do not independently upgrade `pytest-asyncio` outside the official Home Assistant test harness dependency pair.

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

- **Required CI:** Lint (locked Ruff); Tests (current locked HA/official pytest harness and coverage gates); **Minimum HA Compatibility** (fixed SHA-256 hash-locked 2026.11.0 policy: full independent suite with coverage on every run, even when both HA version pins coincide); Package Test (integration-root ZIP layout). The release workflow performs its own full validation. Actionlint/ShellCheck, zizmor, pinned actions and least-privilege tokens are part of workflow hardening.
- **Daily compatibility canary** ([workflow](../.github/workflows/ha-compatibility-canary.yml), 04:47 UTC): Selects the latest matching stable Home Assistant/official pytest pair and runs the full suite in isolation. It reports lag and failures without changing the locked baseline.
- **Weekly normal baseline and minimum checks**: locked tests run Monday 04:23 UTC; minimum policy runs Monday 05:13 UTC. Both also run on PRs, pushes to main, and manual dispatch.
- **Daily baseline updater** ([workflow](../.github/workflows/ha-test-baseline-updater.yml), 06:43 UTC): Proposes an update only for a matched stable Home Assistant/official pytest pair after the **three-day maturity window**. It validates the proposed dependency delta and may open one **review-only PR**. It changes only the tested current baseline, while the supported minimum stays 2026.11.0 and is separately protected. No automerge or automatic release.
- **2026.11 maturity normalization completed:** after the two official 2026.11.0 artifacts passed the 72-hour window on 2026-11-08, the temporary package-level cutoffs were removed from `pyproject.toml` and `uv.lock` in #184; obsolete explanatory comments were removed in #185. The normal `exclude-newer = "3 days"` policy now applies to all packages, with no dependency-version or hash changes. Future exceptional promotions still require a reviewed, bounded waiver.
- **Manual cooldown waiver:** on the updater's `workflow_dispatch`, `override_cooldown=true` on `main` requires a printable 15–200 character reason. Only a compatible stable HA/official pytest pair may be proposed early. The generated review-only PR receives package-specific absolute cutoffs derived from the latest usable release artifacts; `UV_EXCLUDE_NEWER=false` is prohibited. Transitive maturity, lock checks, full tests, coverage, lint and targeted lock-delta review remain mandatory.
- **Normalization:** an ordinary promotion removes the previous scoped exception. When the current locked pair is already mature (even if a newer compatible pair is still cooling), the updater may propose a separate `normalize` PR that removes only `pyproject.toml` and `uv.lock` cutoff metadata, preserving **all package records**. Scheduled runs may propose review-only PRs; `publish=false` on manual dispatch performs a dry planning check. No automerge, and stable releases cannot be prepared while a scoped exception remains.
- **Branch protection:** The [Protect main ruleset](./protect-main-ruleset.md) was activated on 2026-11-07; it requires reviewed-HEAD Actions checks, CodeQL, linear/squash PR history and no force pushes. The release-tag ruleset and native release immutability are also enabled. Keep the blueprint and real repository settings synchronized.
- **Renovate:** PyPI updates respect a three-day release maturity gate; the reviewed HA/official pytest pair is owned by the daily updater, not Renovate's independent bumping. Weekly `uv.lock` maintenance is review-only, grouped GitHub Actions/security tooling receive 3-day maturity checks, and the fixed minimum-HA input/hash lock are excluded from automatic updates. Dependabot version PRs are disabled to avoid competing bots.
- **Package boundary:** Release ZIPs contain only `custom_components/airzoneclouddaikin`. The integration version is in its `manifest.json` and is **independent** of the development tooling version in `pyproject.toml`.

For preparing and publishing a version, use [RELEASING.md](../RELEASING.md). Releases are prepared as drafts and published manually; tests passing is not permission to claim hardware compatibility or to publish automatically.
