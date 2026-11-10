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
