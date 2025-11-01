"""
Helper utilities for optimistic writes and integer clamping.

Why this module?
- DRY: centralize the optimistic TTL window and clamp logic used by multiple platforms.
- UX: avoid transient flicker after a write when the first refresh still returns the
       previous snapshot from the backend.
- Safety: drop optimistic state early if a backend mismatch is explicitly detected.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant

from .const import OPTIMISTIC_TTL_SEC, POST_WRITE_REFRESH_DELAY_SEC


def compute_deadline(
    hass: HomeAssistant,
    *,
    min_ttl: float = OPTIMISTIC_TTL_SEC,
    refresh_delay: float = POST_WRITE_REFRESH_DELAY_SEC,
    margin: float = 2.0,
) -> float:
    """Return a monotonic deadline for optimistic state validity.

    We guarantee at least one full refresh window after a write by taking:
        deadline = now + max(min_ttl, refresh_delay + margin)
    """
    ttl = max(float(min_ttl), float(refresh_delay) + float(margin))
    return hass.loop.time() + ttl


def optimistic_active(hass: HomeAssistant, expires: Optional[float]) -> bool:
    """True if optimistic state is still valid."""
    return expires is not None and hass.loop.time() < float(expires)


def optimistic_set(
    hass: HomeAssistant, store: Dict[str, Any], updates: Dict[str, Any]
) -> float:
    """Update the local optimistic store and return a new expiry deadline."""
    store.update(updates)
    return compute_deadline(hass)


def optimistic_take(
    hass: HomeAssistant,
    expires: Optional[float],
    optimistic_value: Any,
    backend_value: Any,
) -> Any:
    """Return optimistic value while valid; otherwise backend value."""
    return optimistic_value if optimistic_active(hass, expires) else backend_value


def optimistic_clear(store: Dict[str, Any]) -> None:
    """Drop all optimistic keys."""
    store.clear()


def optimistic_drop_if_mismatch(
    hass: HomeAssistant,
    expires: Optional[float],
    store: Dict[str, Any],
    *,
    key: str,
    backend_value: Any,
) -> Optional[float]:
    """If optimistic window is active and the backend disagrees on `key`, drop it.

    Returns the (possibly cleared) expiry timestamp to be stored by caller.
    """
    if not optimistic_active(hass, expires):
        return None
    if key in store and store[key] != backend_value:
        store.clear()
        return None
    return expires


def clamp_int(
    value: int | float,
    *,
    min_value: int,
    max_value: int,
    step: int | None = None,
) -> int:
    """Clamp an integer-like value to [min_value, max_value] and quantize by step.

    - Rounds to nearest step (if provided and >1).
    - Enforces inclusive bounds.
    """
    v = int(round(float(value)))
    if step and step > 1:
        # Quantize to nearest multiple of `step`
        v = int(round(v / step) * step)
    if v < min_value:
        v = min_value
    if v > max_value:
        v = max_value
    return v
