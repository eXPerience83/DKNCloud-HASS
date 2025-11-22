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
