"""Shared finalization for both disposable Home Assistant laboratories."""

import asyncio
import json
from pathlib import Path


async def finish_lab(hass, report, closers, describe):
    """Attempt every resource close and always release the HA stop listener."""
    try:
        cancellation: asyncio.CancelledError | None = None
        for close in closers:
            try:
                await close()
            except asyncio.CancelledError as error:
                # Resource ownership survives cancellation: attempt every
                # remaining closer, then preserve the cancellation signal.
                cancellation = error
                report["error"] = "cleanup_cancelled"
                hass.data["lab_failed"] = True
            except Exception:
                report["error"] = "cleanup_failed"
                hass.data["lab_failed"] = True
        if cancellation is not None:
            raise cancellation
        report.update(describe())
        Path(hass.config.path("lab-result.json")).write_text(json.dumps(report))
    except BaseException:
        hass.data["lab_failed"] = True
        raise
    finally:
        if report.get("error"):
            hass.data["lab_failed"] = True
        hass.data["lab_finished"].set()
