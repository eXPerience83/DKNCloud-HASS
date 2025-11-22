# Repository Guidelines

## Tooling
- Tooling (CI, lint, format) runs on **Python 3.14**; keep code compatible with Home Assistant's minimum Python (currently 3.13), so avoid 3.14-only syntax.
- Format code with **Black 25.9** (line length 88, target-version `py314`); CI pins `black==25.*`.
- Lint and sort imports with **Ruff** targeting `py314`, matching the configuration in `pyproject.toml`; CI pins `ruff==0.14.*`.
- Every change must pass `ruff check --fix --select I` (for import order) and `ruff check` before submission.
- Run `black .` (or the narrowest possible path) to ensure formatting.

## Style and Documentation
- All code comments, README entries, and changelog notes **must be written in English**.
- Keep imports tidy—remove unused symbols and respect the Ruff isort grouping so the Home Assistant package stays first-party under `custom_components/airzoneclouddaikin`.

## Changelog / `CHANGELOG.md`
- Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
- Keep the opening text exactly as: `# Changelog` followed by the standard two-line preamble and an `## [Unreleased]` section before dated releases.
- Version headings use `## [x.y.z] - YYYY-MM-DD` (pre-releases like `1.2.3-rc1` or `1.2.3-alpha1` stay in the brackets). Newer versions go above older ones.
- Allowed categories per release are only `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, and `### Security`. Re-map other themes (Docs, Testing, Tooling, Notes, etc.) into these headings.
- Bullets start with `- `, and continuation lines are indented; keep manual wrapping near 80–100 characters without breaking words. Avoid inserting blank lines inside a bullet.
- Do not reflow or rename existing bullets unless editing their content. Prefer minimal diffs and leave historical notes intact.
- When a final release replaces a pre-release (e.g., `1.2.3` after `1.2.3-rc1`), merge the applicable bullets into the final section and drop the pre-release section unless intentionally preserved.
- Optional comparison links (if present) should match this repo: `[Unreleased]: https://github.com/eXPerience83/DKNCloud-HASS/compare/v<latest>...HEAD` and `[x.y.z]: https://github.com/eXPerience83/DKNCloud-HASS/compare/v<previous>...v<x.y.z>`.

## Integration Architecture
- This repository hosts the custom integration **DKN Cloud for Home Assistant**, distributed through **HACS**.
- Preserve the current coordinator-driven architecture under `custom_components/airzoneclouddaikin` when extending functionality. Study the existing setup (`__init__.py`, platform files, and helpers) and mirror their async patterns, error handling, and notification logic.
- When implementing features, align with Home Assistant best practices (ConfigEntry setup, `DataUpdateCoordinator`, platform separation) and avoid introducing blocking I/O in the event loop.

## Verification
- Ensure the integration still loads within Home Assistant with the existing config flows and maintains parity with the current logic paths for entity updates and notifications.

## Change management
- Do not change the integration version in `manifest.json` or the changelog unless explicitly requested.
- Do not rename entities, adjust unique ID patterns, or modify translation keys unless explicitly requested.
- Prefer minimal, focused diffs; avoid cosmetic refactors or large code moves.
