"""Shared helpers for DKN Cloud for HASS.

Centralized logic for:
- Optimistic state with a grace mismatch (prevents early refresh flicker).
- TTL deadline that always covers at least one coordinator refresh.
- Integer clamping/quantization.

Usage pattern
-------------
from .helper import OptimisticTracker, clamp_int

self._opt = OptimisticTracker(self.hass)
self._opt.set("power", "1")               # after a write
value = self._opt.get("power", backend)   # when reading

# On coordinator updates (new backend snapshot):
self._opt.reconcile(backend_device_dict)  # clears or retains optimistic as needed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from .const import OPTIMISTIC_TTL_SEC, POST_WRITE_REFRESH_DELAY_SEC


def _deadline(hass: HomeAssistant) -> float:
    """Compute a deadline that covers at least one refresh cycle.

    Adds a small guard so the first refresh after a write cannot undo optimism.
    """
    base = float(OPTIMISTIC_TTL_SEC)
    min_cover = float(POST_WRITE_REFRESH_DELAY_SEC) + 2.0
    ttl = max(base, min_cover)
    return hass.loop.time() + ttl


def clamp_int(value: float | int, vmin: int, vmax: int, step: int = 1) -> int:
    """Clamp + quantize to integer respecting bounds and step."""
    ival = int(round(float(value) / step) * step)
    if ival < vmin:
        ival = vmin
    if ival > vmax:
        ival = vmax
    return ival


@dataclass(slots=True)
class _State:
    values: dict[str, Any] = field(default_factory=dict)
    # Allow one backend mismatch per key within the TTL to ignore the very first stale refresh.
    grace_used: set[str] = field(default_factory=set)
    valid_until: float | None = None


class OptimisticTracker:
    """Keep short-lived optimistic values with a single-grace mismatch policy."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._st = _State()

    # ---- core API --------------------------------------------------------

    def active(self) -> bool:
        """Return True if optimism is currently valid."""
        return bool(self._st.valid_until and self._hass.loop.time() < self._st.valid_until)

    def set(self, key: str, value: Any) -> None:
        """Set optimistic value for a field and (re)arm TTL."""
        self._st.values[key] = value
        if key in self._st.grace_used:
            self._st.grace_used.remove(key)
        self._st.valid_until = _deadline(self._hass)

    def get(self, key: str, backend_value: Any) -> Any:
        """Get optimistic value for key if active; otherwise backend value."""
        if self.active() and key in self._st.values:
            return self._st.values[key]
        return backend_value

    def clear(self) -> None:
        """Drop all optimistic values immediately."""
        self._st.values.clear()
        self._st.grace_used.clear()
        self._st.valid_until = None

    # ---- coordinator reconciliation -------------------------------------

    def reconcile(self, backend: dict[str, Any]) -> None:
        """Compare backend with optimistic state and decide to keep or drop it.

        Policy:
          - If TTL expired → clear all.
          - While active:
              For each optimistic key:
                * If backend differs the first time → tolerate, mark grace_used.
                * If differs again within TTL → clear all optimistic (backend wins).
              If all optimistic keys match → we can optionally keep until TTL or
              let TTL expire naturally; we leave it as-is (safe).
        """
        if not self.active():
            self.clear()
            return

        # If there is no optimistic content, keep TTL alive to cover chained writes.
        if not self._st.values:
            return

        second_mismatch_detected = False

        for key, opt_val in list(self._st.values.items()):
            back_val = backend.get(key)
            if _norm(back_val) == _norm(opt_val):
                # Match → nothing to do for this key
                continue

            # First mismatch allowed inside TTL
            if key not in self._st.grace_used:
                self._st.grace_used.add(key)
                continue

            # Second mismatch for the same key → backend wins
            second_mismatch_detected = True
            break

        if second_mismatch_detected:
            self.clear()


def _norm(v: Any) -> str:
    """Normalize values for robust comparisons."""
    if v is None:
        return ""
    try:
        s = str(v).strip()
        # Collapse identical numeric forms like "21" vs "21.0"
        if s.replace(".", "", 1).isdigit():
            try:
                return str(int(float(s)))
            except Exception:
                return s
        return s.lower()
    except Exception:  # noqa: BLE001
        return ""
