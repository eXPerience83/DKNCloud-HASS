"""Cleanup failures cannot keep a disposable HA process waiting forever."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.lab_lifecycle import finish_lab


@pytest.mark.parametrize("failure", ["cleanup", "metadata", "write"])
async def test_finish_event_and_remaining_closes_survive_failure(tmp_path, failure):
    hass = SimpleNamespace(
        data={"lab_finished": asyncio.Event()},
        config=SimpleNamespace(path=lambda name: str(tmp_path / name)),
    )
    first, second = AsyncMock(), AsyncMock()
    if failure == "cleanup":
        first.side_effect = RuntimeError("synthetic")
    if failure == "write":
        (tmp_path / "lab-result.json").mkdir()

    def describe():
        if failure == "metadata":
            raise RuntimeError("synthetic")
        return {"closed": True}

    if failure == "cleanup":
        await finish_lab(hass, {}, [first, second], describe)
    else:
        with pytest.raises((RuntimeError, IsADirectoryError)):
            await finish_lab(hass, {}, [first, second], describe)
    first.assert_awaited_once()
    second.assert_awaited_once()
    assert hass.data["lab_finished"].is_set()
    assert hass.data["lab_failed"]


async def test_finish_lab_continues_closing_after_cancellation(tmp_path):
    """Even cancellation of the first resource must close the second."""
    hass = SimpleNamespace(
        data={"lab_finished": asyncio.Event()},
        config=SimpleNamespace(path=lambda name: str(tmp_path / name)),
    )
    first = AsyncMock(side_effect=asyncio.CancelledError())
    second = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await finish_lab(hass, {}, [first, second], lambda: {"closed": True})

    first.assert_awaited_once()
    second.assert_awaited_once()
    assert hass.data["lab_finished"].is_set()
    assert hass.data["lab_failed"]
    assert not (tmp_path / "lab-result.json").exists()


async def test_cancelled_protected_scenario_finalizes_transports(monkeypatch, tmp_path):
    """Cancel the real scenario task before its finalization phase."""
    import logging
    from contextlib import nullcontext

    from custom_components.airzoneclouddaikin import airzone_api
    from scripts import protected_ha_lab

    finished = asyncio.Event()
    login_entered = asyncio.Event()
    registered = {}
    original_handlers = list(logging.getLogger().handlers)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "synthetic-test")

    async def close_session():
        session.closed = True

    async def close_backend():
        backend.runner.sites.clear()

    session = SimpleNamespace(closed=False, close=AsyncMock(side_effect=close_session))
    gateway = SimpleNamespace(
        protected=SimpleNamespace(session=session),
        observations={},
        enforce=nullcontext,
    )
    backend = SimpleNamespace(
        directory=str(tmp_path),
        start=AsyncMock(),
        connector=lambda **_: object(),
        wait=None,
        operations={},
        controls=0,
        runner=SimpleNamespace(sites={"synthetic"}),
        close=AsyncMock(side_effect=close_backend),
    )
    backend.start.return_value = backend

    class PausedLogin:
        def __init__(self, *_args, **_kwargs):
            pass

        async def login(self):
            login_entered.set()
            await asyncio.Event().wait()
            return True

    fake_hass = SimpleNamespace(
        data={"lab_finished": finished},
        config=SimpleNamespace(
            config_dir=str(tmp_path),
            path=lambda name: str(tmp_path / name),
        ),
        bus=SimpleNamespace(
            async_listen_once=lambda event, callback: registered.update(
                event=event, callback=callback
            )
        ),
    )
    monkeypatch.setattr(airzone_api, "AirzoneAPI", PausedLogin)
    monkeypatch.setattr(
        protected_ha_lab, "SyntheticBackend", lambda _directory: backend
    )
    monkeypatch.setattr(
        protected_ha_lab, "create_gateway", AsyncMock(return_value=gateway)
    )

    try:
        assert await protected_ha_lab.async_setup(fake_hass, {})
        assert registered["event"] == "homeassistant_started"
        task = asyncio.create_task(registered["callback"](None))
        await asyncio.wait_for(login_entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        session.close.assert_awaited_once()
        backend.close.assert_awaited_once()
        assert session.closed and not backend.runner.sites
        assert finished.is_set()
        assert fake_hass.data["lab_failed"]
        assert (tmp_path / "lab-result.json").exists()
    finally:
        if restore := fake_hass.data.get("lab_restore_guard"):
            restore()
        logging.getLogger().handlers = original_handlers


async def test_legacy_entry_constructor_failure_releases_real_lab(
    hass, monkeypatch, socket_enabled, tmp_path
):
    import json
    from pathlib import Path

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    import custom_components.airzoneclouddaikin as integration
    from scripts import isolated_ha_lab

    # Track restoration of the laboratory-only session replacement.
    monkeypatch.setattr(
        integration, "async_get_clientsession", integration.async_get_clientsession
    )

    def fail_constructor(**_):
        raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(isolated_ha_lab, "ConfigEntry", fail_constructor)
    hass.config.config_dir = str(tmp_path)
    hass.data["lab_finished"] = asyncio.Event()
    assert await isolated_ha_lab.async_setup(hass, {})
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    async with asyncio.timeout(5):
        await hass.data["lab_finished"].wait()
    report = json.loads(Path(hass.config.path("lab-result.json")).read_text())
    assert report["error"] == "RuntimeError"
    assert report["session_closed"] and report["server_closed"]
    assert hass.data["lab_failed"]
