"""Minimal independent HA process; no pytest fixture or global HA installation."""

import asyncio
import sys

from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity,
    entity_registry as er,
    frame,
    restore_state as rs,
)
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM


async def main():
    hass = HomeAssistant(sys.argv[1])
    hass.config.skip_pip = True
    hass.config.units = METRIC_SYSTEM
    await hass.config.async_set_time_zone("UTC")
    loader.async_setup(hass)
    entity.async_setup(hass)
    frame.async_setup(hass)
    finished = asyncio.Event()
    hass.data["lab_finished"] = finished
    config = {"isolated_ha_lab": {}}
    hass.config_entries = config_entries.ConfigEntries(hass, config)
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await ar.async_load(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    await rs.async_load(hass)
    if not await async_setup_component(hass, "isolated_ha_lab", config):
        raise RuntimeError("Synthetic lab setup failed")

    async def stop_when_finished():
        await finished.wait()
        await hass.async_stop()

    stopper = asyncio.create_task(stop_when_finished())
    await hass.async_run()
    await stopper


asyncio.run(main())
