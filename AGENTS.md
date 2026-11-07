# Repository Guidelines

## Tooling
- Tooling (CI, lint, format) runs on **Python 3.14**. The `pyproject.toml` targets packaging/tooling and also pins `requires-python = ">=3.14"`. All code under `custom_components/airzoneclouddaikin/` targets Python 3.14+, matching the Home Assistant 2026.11.0 minimum supported release.
- Format code and lint/sort imports with Ruff (line length 88, target-version `py314`), using the exact version pinned in `pyproject.toml` and `uv.lock`.
- For local fixes, run `uv run --locked ruff check --fix --select I .` and then `uv run --locked ruff check .`.
- Run `uv run --locked ruff format .` (or a narrower path) to format code; CI checks with `ruff format --check`.
- The test harness uses the exact Python 3.14 patch in `.python-version` and the pytest version required by official `pytest-homeassistant`.
- This project uses Python 3.14 syntax intentionally. PEP 758 allows multiple
  exceptions without parentheses when no `as` clause is present (e.g.
  `except TypeError, ValueError:`). Reviewers must not flag or rewrite this
  syntax for Python 3.13 or earlier compatibility.
- `homeassistant`, `pytest-homeassistant` (official pytest-homeassistant), and `pytest`
  form a coordinated, exactly pinned test baseline. Update Home Assistant and
  official pytest-homeassistant through the reviewed baseline updater; never bump pytest independently
  of the official test harness.
- `uv.lock` is authoritative: CI uses `uv lock --check` and locked syncs. Preserve
  `exclude-newer = "3 days"` and Renovate's matching minimum-release-age gate.
  The CI Python interpreter stays on the approved 3.14 patch series; advancing
  to Python 3.15 needs separate Home Assistant/official pytest-homeassistant compatibility review.

## GitHub Actions security
- Pin external GitHub Actions to audited full 40-character commit SHAs, with a
  trailing version comment for maintainability. Use the same SHA set as the
  corresponding reviewed Pollen Levels workflows when appropriate; validate
  every change rather than following floating branches or tags in CI.
- `workflow-security.yml` runs actionlint (with ShellCheck) and zizmor on PRs,
  main, and manual dispatch. Review all findings before merging; avoid blanket
  suppressions.
- Use the minimum GITHUB_TOKEN permissions per workflow or job. Keep checkout
  `persist-credentials: false` unless a narrowly scoped, documented write
  operation requires otherwise. Do not expose secrets to untrusted PR code.
- Preserve DKN-specific package validation and HA/official pytest-homeassistant updater protections.
  Release workflow redesign is tracked in #141 and is not part of security
  baseline refactoring.

## CI workflow responsibilities
- Keep Lint on the locked `lint` group (Ruff only); Tests uses the locked
  `test` group and owns the HA/official pytest-homeassistant full test suite and coverage gates.
- Package Test is intentionally limited to building and validating the exact
  `airzoneclouddaikin.zip` archive. Do not duplicate the full test suite or
  install the HA harness in that workflow; the PR's Tests job covers it.
- Retain a fully validated release path and DKN's Marketplace package
  contract; release changes must be coordinated with #141.
- Prefer `pull_request` scoped to `main` and concurrency keys using the
  workflow and PR number (or `github.ref`) to avoid cross-fork collisions.
- Preserve the separate scheduled/manual latest-HA canary and reviewed
  baseline updater, and the independent hash-locked minimum-HA workflow.
  Keep the 2026.11.0 minimum protected when the main test baseline advances.
- Maintain the documented main ruleset requirements, including the independent
  `minimum-compatibility` job when it is required. A repository administrator
  must activate GitHub settings; a JSON blueprint alone does not enforce them.
- The manual updater `override_cooldown` is a reasoned single-run bypass for
  the exact official HA/pytest pair only. Use absolute, artifact-bounded UTC
  per-package cutoffs, never `UV_EXCLUDE_NEWER=false` or an open-ended `false`
  package exemption. Normalize after maturity and reject stable releases while
  an exception is present. Keep full checks, locked tests and PR review.

## Release procedure
- Follow `RELEASING.md` and #141. Draft preparation is automated; publication
  is always manual and requires a maintainer's review.
- The integration version is taken from the manifest and the first changelog
  heading. `pyproject.toml` tracks separate development tooling; do not
  synchronize its version to the manifest without a tooling reason.
- Release must be dispatched from `main`, while `release_ref` selects a
  verified same-repository commit. Validate with locked Python, Ruff, tests,
  and the DKN Marketplace ZIP validator. Only finalization has write scope.
- Never overwrite published ZIP assets or move published tags. Preserve
  `v0.4.8` and `v0.5.0a4`; verify alpha/beta/RC status before publication.

## Release & API boundaries
- Do not change the integration version or changelog entries unless explicitly requested.
- Do not rename entities, alter `unique_id` patterns, or modify translation keys unless explicitly requested.
- Prefer minimal, focused diffs; avoid cosmetic refactors or large code moves.

## Test and Defensive Coding Policy
- Do not add defensive code or tests only because an arbitrary JSON shape could theoretically exist.
- Add defensive handling or regression tests when they:
  - protect public behavior or user-facing outputs,
  - cover a real bug or previously observed failure,
  - cover a new branch introduced by the change,
  - prevent a likely exception from partially malformed upstream data,
  - or document an intentional compatibility or migration behavior.
- Prefer small, focused tests over large synthetic fixtures.
- Avoid duplicating malformed-payload test cases when the same behavior is already covered by a smaller helper-level test.
- Keep defensive parsing proportional to the upstream API contract; do not add broad schema validators unless the integration has observed real instability in that payload area.
- When a review suggests hardening for a highly unlikely upstream shape, first decide whether the scenario is realistic enough to justify code and test complexity. If not, document the reasoning in the PR instead of expanding the code.

## Changelog / `CHANGELOG.md`
- This section is the single source of truth for the changelog style. Ignore external style guides when they conflict with these rules.
- Do NOT add any new boilerplate, intro text or `## [Unreleased]` section automatically. If such sections already exist, leave them and only edit their content when explicitly requested.
- Version headings must use the pattern `## [version] - YYYY-MM-DD` (ASCII hyphen) and releases must stay in reverse chronological order. Use SemVer identifiers such as `1.8.2`, `1.8.2-rc1`, `1.8.1-alpha1`.
- Inside each version, use only these headings: `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`. Prefer `Changed` for documentation-only updates.
- For breaking changes, keep one of the allowed section headings above and
  prefix the bullet with `**Breaking change:**`; do not add a separate
  `### Breaking Changes` heading.
- Each change must be a `- ` bullet. Wrap long bullets around 80–100 characters with indented continuation lines; do NOT insert blank lines inside a bullet and avoid `<br>` or trailing double spaces.
- Keep diffs minimal: never reflow or rewrap unrelated text, and never rename existing headings unless they clearly violate these rules.
- If comparison links exist at the bottom of the changelog, keep the existing style and only extend it for new versions of this repository.

## Style and Documentation
- All code comments, README entries, and changelog notes **must be written in English**.
- Keep imports tidy—remove unused symbols and respect the Ruff isort grouping so the Home Assistant package stays first-party under `custom_components/airzoneclouddaikin`.
- The translation source of truth is `custom_components/airzoneclouddaikin/translations/en.json`.
- Keep all other locale files synchronized with that keyset.
- Do not add or rely on a `strings.json` file.
- Do not introduce `%key:` translation references in this custom repository.

## Backend Contract (`info.md`)
- The backend contract and observed behavior that this integration relies on is documented in `info.md`.
- Treat `info.md` as the single source of truth for request shapes, accepted value ranges, mode mappings/aliases, and sleep/scene semantics.
- Any code change that affects API I/O or entity behavior must be reflected in `info.md` (and in user-facing docs when applicable).

## Integration Architecture
- This repository hosts the custom integration **DKN Cloud for Home Assistant**, distributed through **HACS / Home Assistant Marketplace**.
- Use **DKN Cloud for Home Assistant** in the integration manifest, HACS metadata, all translation root titles, and current documentation. Identify the project as an **unofficial integration** and retain the README's independence and trademark disclaimer.
- Keep the Daikin manufacturer icons previously approved for this integration unchanged. The display name must not alter the `airzoneclouddaikin` domain, existing unique IDs, repository name, or ZIP archive name.
- Preserve the current coordinator-driven architecture under `custom_components/airzoneclouddaikin` when extending functionality. Study the existing setup (`__init__.py`, platform files, and helpers) and mirror their async patterns, error handling, and notification logic.
- When implementing features, align with Home Assistant best practices (ConfigEntry setup, `DataUpdateCoordinator`, platform separation) and avoid introducing blocking I/O in the event loop.

## Verification
- Ensure the integration still loads within Home Assistant with the existing config flows and maintains parity with the current logic paths for entity updates and notifications.
- Before submitting changes, follow the same locked toolchain as CI:
  - `uv lock --check` — ensure `pyproject.toml` and `uv.lock` agree.
  - `uv sync --locked --only-group release` — install the pinned lint and test environment.
  - `uv run --locked --no-sync ruff check .` — lint and import ordering.
  - `uv run --locked --no-sync ruff format --check .` — formatting verification.
  - `uv run --locked --no-sync python -m pytest -q` — unit tests.
