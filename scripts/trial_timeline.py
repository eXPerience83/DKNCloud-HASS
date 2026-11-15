"""Allowlisted monotonic timeline for supervised LAB measurements.

Never serializes device identities, account data, URLs, tokens or raw frames.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

_ALLOWED = {
    "subscription_ack",
    "control_invoked",
    "control_admitted",
    "control_result",
    "info_admitted",
    "info_result",
    "pusher_received",
    "get_admitted",
    "get_applied",
    "ha_optimistic",
    "ha_confirmed",
    "trial_stopped",
}
_FIELDS = {"cold_consign", "heat_consign"}
_SOURCES = {"pusher", "get", "ha", "transport", "lab"}
_RESULTS = {"accepted", "ambiguous", "rejected", "unavailable", "confirmed"}


def _sanitized_temperature(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("No valid setpoint value")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Non-numeric setpoint") from error
    if (
        not decimal.is_finite()
        or decimal != decimal.to_integral_value()
        or not 0 <= decimal <= 60
    ):
        raise ValueError("Invalid setpoint for anonymous timeline")
    return int(decimal)


class TrialTimeline:
    """Preserve ordering and provenance, excluding all unapproved information."""

    def __init__(self, *, clock=time.monotonic_ns):
        self._clock = clock
        self._origin = clock()
        self._events: list[dict] = []

    def add(
        self,
        kind: str,
        *,
        field: str | None = None,
        value: object = None,
        source: str | None = None,
        result: str | None = None,
    ) -> None:
        if kind not in _ALLOWED or (
            source is not None and source not in _SOURCES
        ) or (result is not None and result not in _RESULTS):
            raise ValueError("Unapproved timeline event")
        record = {"kind": kind, "elapsed_ms": (self._clock() - self._origin) / 1e6}
        if field is not None:
            if field not in _FIELDS:
                raise ValueError("Unapproved timeline field")
            record["field"] = field
            record["value_c"] = _sanitized_temperature(value)
        elif value is not None:
            raise ValueError("Unscoped timeline value")
        if source is not None:
            record["source"] = source
        if result is not None:
            record["result"] = result
        self._events.append(record)

    def record_push_patch(self, patch: dict, *, expected_field: str) -> None:
        """Record an actual inbound Pusher field before coordinator merging."""
        if expected_field not in _FIELDS:
            raise ValueError("Unapproved trial field")
        if expected_field in patch and patch[expected_field] is not None:
            self.add(
                "pusher_received",
                field=expected_field,
                value=patch[expected_field],
                source="pusher",
            )

    def export(self) -> list[dict]:
        return [dict(event) for event in self._events]
