# Local DKN Cloud validation with Codex and Remote Dev

This runbook uses the existing [Remote Dev Containers](https://github.com/eXPerience83/remote-dev-containers)
project on TrueNAS SCALE. It complements, but does not replace, the public
GitHub Actions checks or [real-device validation](real-device-validation.md).

## Verified source assumptions (2026-10-30)

- Remote Dev `main` uses Python **3.14.10** by default, `uv 0.13.6`, and
  Ruff **0.17.3** (reviewed upstream refresh #294). The globally supplied
  `pytest` is **9.1.1** and is **not** DKN's test harness; DKN locks its own
  pytest, Home Assistant and PHACC in `uv.lock`.
- DKN's locked test pair is Home Assistant **2026.10.4** / PHACC **0.13.375**,
  running on Python **3.14.10**.
- Remote Dev's canonical TrueNAS configuration is `compose/truenas.yml`.
  The reference Codex host workspace is
  `/mnt/Pool1/remote-dev/workspaces/codex`, mounted in the Codex role as
  `/workspace`. A DKN checkout therefore belongs at
  `/workspace/DKNCloud-HASS` (one validated direct child).
- The deployed image digest, running services, accessible host mounts and
  GitHub authentication **have not been established by this document**.
  Inspect the actual TrueNAS deployment and run `remote-dev-doctor` within
  the Codex role before trusting any source-version assumptions.

The TrueNAS host must provide the administrator-created Generic/POSIX data
root and use the same remote-dev source revision for bootstrap, preflight,
ACL audit and deployment. Do not mount the Docker socket, TrueNAS root or
shared agent credentials merely to run DKN tests.

## Open the DKN project in the Codex role

In the protected Codex terminal, select or create the `DKNCloud-HASS` project
from the Projects menu. Work inside its private checkout directory. If the
new project directory is **empty**, clone the public repository into that
directory; never overwrite an existing checkout:

```sh
cd /workspace/DKNCloud-HASS
# Run this clone command ONLY when the current directory is empty:
git clone https://github.com/eXPerience83/DKNCloud-HASS.git .
git status --short --branch
git rev-parse HEAD
remote-dev-doctor
```

For a pre-existing checkout, **skip the clone command** and inspect its branch,
remote and uncommitted files before fetching or changing branches. Project
selection changes working directory; it does **not** isolate sibling projects
mounted in the same role.

## Confirm the exact project toolchain

Run these from the DKN checkout:

```sh
python --version
uv --version
test "$(python --version)" = "Python 3.14.10"
uv --version | grep -Eq '^uv 0[.]13[.]6([[:space:]]|$)'
cat .python-version
uv lock --check
uv sync --locked --only-group release
uv run --locked --no-sync python -c 'import importlib.metadata as m; print({p: m.version(p) for p in ("homeassistant", "pytest-homeassistant-custom-component", "pytest", "ruff")})'
```

Expected locked versions at this revision are HA 2026.10.4, PHACC 0.13.375,
pytest 9.0.3 and Ruff 0.17.3. **Use the project-specific `uv run` commands**;
running the image's global `pytest` can invoke an incompatible harness.
Do not automatically replace project pins with bundled CLI versions. If the
image/tool versions differ, inspect its source digest and the current DKN lock
before modifying either repository.

Normal Remote Dev sessions place `TMPDIR` and `UV_CACHE_DIR` inside the
Codex role's private persistent `/workspace/.remote-dev-tmp` scratch area.
Keep these defaults. Do not store tokens, API captures or credentials there.

## Reproduce the required checks

```sh
uv lock --check
uv run --locked --no-sync ruff check .
uv run --locked --no-sync black --check .
uv run --locked --no-sync python -m compileall -q custom_components tests
PYTHONPATH=. uv run --locked --no-sync python -m pytest -q
```

Focused HTTP and coordinator coverage uses synthetic responses and loopback
test servers, with no real DKN credentials:

```sh
PYTHONPATH=. uv run --locked --no-sync python -m pytest -q \
  tests/test_airzone_api.py tests/test_coordinator_lifecycle_contract.py \
  tests/test_config_flow.py
```

The full suite includes Home Assistant fixtures; it is **not** equivalent to
testing an installed integration on an HA server or a physical DKN unit.
Coverage and the project thresholds are checked in the required
`.github/workflows/tests.yml` job.

To reproduce the integration-root installable ZIP validation, from a committed
revision (working-tree edits are deliberately excluded by `git archive`):

```sh
git archive --format=zip --output=airzoneclouddaikin.zip \
  HEAD:custom_components/airzoneclouddaikin
uv run --locked --no-sync python scripts/validate_marketplace_release.py \
  airzoneclouddaikin.zip
```

Keep the generated archive local; do not commit it. Review the branch and
commit being archived before using it for an isolated test installation.

## What remains outside these local checks

- **Home Assistant 2026.11 beta:** use a separate disposable environment
  only after identifying a matching 2026.11 PHACC harness and explicitly
  recording the tested versions. The scheduled canary intentionally excludes
  prereleases. A beta UI running in a real HA server is not by itself a
  reproducible CI test harness.
- **Backend behavior / issue #101:** loopback tests can prove request shapes,
  error paths and asynchronous scheduling, but cannot establish when
  `GET /devices` changes after app/physical controls or whether
  `cgi=infomaquina` improves convergence. Follow the controlled A/B
  observations in [real-device-validation.md](real-device-validation.md).
  Do not merge #102 or its test-only staging PR #127 based on synthetic tests.
- **Private authenticated probes:** use credentials only in a locally isolated,
  approved harness. Never put tokens, emails, MACs, PINs, installation IDs,
  raw traffic, secrets or unredacted output in public CI/PR artifacts.
  Issue #55 still tracks the authenticated PowerShell harness.
- **Actual TrueNAS availability:** this runbook does not claim access to the
  host, active containers, credentials, or the installed image. Verify those
  at runtime; if access is missing, report the blocker rather than asserting
  that local tests ran.

Report exact project/remote-dev commits, interpreter and package versions,
commands, exit codes, meaningful failures and any CI link. Distinguish
*offline synthetic PASS*, *installed HA PASS* and *real device PASS*.
