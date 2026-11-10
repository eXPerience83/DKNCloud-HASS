"""Shared finalization for both disposable Home Assistant laboratories."""

import json
from pathlib import Path


async def finish_lab(hass, report, closers, describe):
    """Attempt every resource close and always release the HA stop listener."""
    try:
        for close in closers:
            try:
                await close()
            except Exception:
                report["error"] = "cleanup_failed"
                hass.data["lab_failed"] = True
        report.update(describe())
        Path(hass.config.path("lab-result.json")).write_text(json.dumps(report))
    except BaseException:
        hass.data["lab_failed"] = True
        raise
    finally:
        if report.get("error"):
            hass.data["lab_failed"] = True
        hass.data["lab_finished"].set()
