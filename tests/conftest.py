"""Shared fixtures for DKN Cloud Home Assistant tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.airzoneclouddaikin.config_flow import (
    CONF_EXPOSE_PII,
    CONF_SCAN_INTERVAL,
)
from custom_components.airzoneclouddaikin.const import (
    CONF_ENABLE_HEAT_COOL,
    CONF_SLEEP_CLEANUP_ENABLED,
    DOMAIN,
)


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
            CONF_SLEEP_CLEANUP_ENABLED: True,
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
    def suspend(self, *, auth=True):
        """Mirror the entry-lifetime API fence in legacy fixtures."""
        pass

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


@pytest.fixture(autouse=True)
def isolate_background_push_in_legacy_contract_tests(monkeypatch):
    """Keep REST/entity unit fixtures isolated from automatic background I/O.

    test_sync_runtime explicitly supplies its connector and exercises the complete
    HA lifecycle. Adapter wire contracts are tested separately with synthetic data.
    """
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.build_verified_adapter",
        lambda api, session: None,
    )

@pytest.fixture
def protected_launcher(tmp_path, socket_enabled):
    """Launch and verify one unchanged supervised synthetic HA scenario."""

    def run_case(case):
        """The final launcher owns the HA child, TLS backend and cleanup in every case."""
        import json
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/run_isolated_ha.py"),
                "--protected",
                "--protected-case",
                case,
                "--process-timeout",
                "140" if case in {"reconnect", "reconnect_no_events"} else "75",
            ],
            cwd=root,
            env={"TMPDIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=150 if case in {"reconnect", "reconnect_no_events"} else 85,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        summary = json.loads(result.stdout.splitlines()[0])
        assert summary.get("error") != "cleanup_failed"
        assert summary["backend_control_writes"] == 0
        if case == "sleep":
            assert summary["observations"]["blocked_control_attempts"] >= 1
        assert summary["session_closed"] and summary["server_closed"]
        assert summary["storage_files"] == 0
        if case in {"no_events", "invalid_event", "wrong_device"}:
            assert summary["pusher_result"] == "inconclusive"
            assert summary["observations"]["device_applied"] == 0
            assert summary["observations"]["get_received"] > 0
            assert summary["pending_http_tasks"] == 0
        if case == "normal":
            assert summary["observations"]["device_changed_state"] >= 1
            assert summary["observations"]["device_applied"] >= 1
            assert summary["observations"]["entity_observed_push"] >= 1
        if case == "no_events":
            assert summary["observations"]["subscribed"]
            assert summary["observations"]["websocket_established"] > 0
        if case == "reconnect_no_events":
            assert not summary["push_recovered"]
        if case == "authorization":
            assert summary["operations"].get("authorize", 0) >= 1
            assert summary["inflight_authorization_at_deadline"]
        if case == "get":
            assert summary["operations"].get("get", 0) >= 1
            assert summary["inflight_get_at_deadline"]
            assert summary["pending_recovery_get_reached"]
            assert summary["get_pending_before_owner_shutdown"]
            assert summary["pending_http_tasks"] == 0
            assert summary["transports_open_when_checked"]
        if case == "cooldown":
            assert summary["cooldown_wait"]
            assert summary["cooldown_pending_at_deadline"]
        if case in {"reconnect", "reconnect_no_events"}:
            assert summary["tls_fallback"] and summary["tls_reconnected"]
        if case in {"normal", "reconnect", "sleep"}:
            assert summary["owned_tasks_closed"]
            assert summary["entities"] >= 5
        assert not list(tmp_path.iterdir())
        for secret in [
            "synthetic-password",
            "synthetic-token",
            "synthetic@example.invalid",
        ]:
            assert secret not in result.stdout + result.stderr


    return run_case
