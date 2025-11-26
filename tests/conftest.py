"""Shared pytest configuration for DKN Cloud for HASS tests.
This file wires in the Home Assistant testing plugin when available and
enables custom integrations for tests that rely on actual HA fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# Ensure the repository root is on sys.path so that imports like
# `custom_components.airzoneclouddaikin` work in all environments.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Try to load the pytest-homeassistant-custom-component plugin.
# - In GitHub Actions / local dev (with requirements_test.txt installed),
#   the plugin will be present and we can use the official HA fixtures.
# - In Codex or other bare environments, the import will fail and the
#   rest of the tests (which use manual stubs) will continue to work.
try:
    import pytest_homeassistant_custom_component as _pytest_ha_cc  # noqa: F401

    HAS_PYTEST_HA = True
except ModuleNotFoundError:
    HAS_PYTEST_HA = False
else:
    # Register the plugin with pytest when available.
    pytest_plugins = ("pytest_homeassistant_custom_component",)

if HAS_PYTEST_HA:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(
        enable_custom_integrations: None,
    ) -> None:
        """Automatically enable custom integrations for HA-based tests.

        The `enable_custom_integrations` fixture is provided by the
        pytest-homeassistant-custom-component plugin. Making this fixture
        autouse ensures that our custom component is discoverable by
        Home Assistant during tests.
        """

        # The fixture has effect just by being requested; nothing else to do.
        yield


if TYPE_CHECKING:
    # Type-checkers can see HomeAssistant for annotations, but this import
    # is not required at runtime when Home Assistant is not installed.
    from homeassistant.core import HomeAssistant as _HomeAssistant  # noqa: F401
