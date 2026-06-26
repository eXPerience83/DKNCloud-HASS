"""Shared fixtures for DKN Cloud Home Assistant tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airzoneclouddaikin.config_flow import (
    CONF_EXPOSE_PII,
    CONF_SCAN_INTERVAL,
)
from custom_components.airzoneclouddaikin.const import (
    CONF_ENABLE_HEAT_COOL,
    CONF_SLEEP_TIMEOUT_ENABLED,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Enable loading custom integrations in every Home Assistant harness test."""
    try:
        request.getfixturevalue("enable_custom_integrations")
    except pytest.FixtureLookupError:
        return


@pytest.fixture
def fake_email() -> str:
    """Return a stable fake email address."""
    return "user@example.com"


@pytest.fixture
def fake_password() -> str:
    """Return a stable fake password."""
    return "not-a-real-password"


@pytest.fixture
def fake_token() -> str:
    """Return a stable fake Airzone token."""
    return "test-token"


@pytest.fixture
def dkn_config_entry_factory(
    fake_email: str,
    fake_token: str,
) -> Callable[..., MockConfigEntry]:
    """Return a factory for DKN MockConfigEntry instances."""

    def _factory(
        *,
        entry_id: str = "dkn-entry",
        email: str = fake_email,
        token: str = fake_token,
        title: str | None = None,
        options: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        unique_id: str | None = None,
        version: int = 2,
    ) -> MockConfigEntry:
        entry_options = {
            "user_token": token,
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
            CONF_ENABLE_HEAT_COOL: False,
            CONF_SLEEP_TIMEOUT_ENABLED: False,
        }
        if options:
            entry_options.update(options)

        entry_data = {CONF_USERNAME: email}
        if data:
            entry_data.update(data)

        return MockConfigEntry(
            domain=DOMAIN,
            entry_id=entry_id,
            title=title or email,
            data=entry_data,
            options=entry_options,
            unique_id=unique_id if unique_id is not None else email.casefold(),
            version=version,
        )

    return _factory


@pytest.fixture
def sample_device() -> dict[str, Any]:
    """Return a representative Airzone device snapshot."""
    return {
        "id": "dev1",
        "name": "Living Room",
        "mac": "AA:BB:CC:DD:EE:FF",
        "brand": "Airzone DKN",
        "firmware": "1.0.0",
        "modes": "11111",
        "mode": "1",
        "power": "1",
        "scenary": "occupied",
        "local_temp": "22.0",
        "cold_consign": "24.0",
        "heat_consign": "20.0",
        "cold_speed": "2",
        "heat_speed": "2",
        "availables_speeds": "3",
        "sleep_time": 30,
        "min_temp_unoccupied": 16,
        "max_temp_unoccupied": 28,
        "connection_date": "2024-01-01T12:00:00+00:00",
    }


@pytest.fixture
def sample_devices(sample_device: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a coordinator-style mapping of device id to device snapshot."""
    return {str(sample_device["id"]): dict(sample_device)}


class FakeAirzoneAPI:
    """Small fake API object for setup/coordinator tests."""

    def __init__(
        self,
        username: str | None = None,
        session: Any | None = None,
        *,
        password: str | None = None,
        token: str | None = None,
        installations: list[dict[str, Any]] | None = None,
        devices_by_installation: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.username = username
        self.session = session
        self.password = password
        self.token = token
        self.fetch_installations = AsyncMock(return_value=installations or [])
        self.fetch_devices = AsyncMock(
            side_effect=lambda installation_id: (devices_by_installation or {}).get(
                str(installation_id), []
            )
        )
        self.async_set_scenary = AsyncMock()
        self.put_device_fields = AsyncMock()
        self.send_event = AsyncMock()

    async def login(self) -> bool:
        """Pretend login succeeds when a token is available."""
        return bool(self.token)

    def clear_password(self) -> None:
        """Drop the fake password."""
        self.password = None


@dataclass
class DummyCoordinator:
    """Minimal coordinator for direct entity unit tests."""

    data: dict[str, dict[str, Any]]
    hass: HomeAssistant
    api: Any | None = None
    refreshes: int = 0
    listeners: list[Callable[..., None]] = field(default_factory=list)

    def async_add_listener(
        self,
        listener: Callable[..., None],
        _context: Any | None = None,
    ) -> Callable[[], None]:
        """Register a listener and return an unsubscribe callback."""
        self.listeners.append(listener)

        def _unsub() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return _unsub

    async def async_request_refresh(self) -> None:
        """Record refresh requests."""
        self.refreshes += 1


@pytest.fixture
def dummy_coordinator_factory(
    hass: HomeAssistant,
) -> Callable[[dict[str, dict[str, Any]], Any | None], DummyCoordinator]:
    """Return a factory for direct entity coordinator fakes."""

    def _factory(
        data: dict[str, dict[str, Any]],
        api: Any | None = None,
    ) -> DummyCoordinator:
        return DummyCoordinator(data=data, hass=hass, api=api)

    return _factory
