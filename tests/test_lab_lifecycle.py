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
