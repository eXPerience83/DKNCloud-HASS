"""Session evidence for automatic Sleep cleanup."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .const import SCENARY_SLEEP, SLEEP_CLEANUP_SETTLE_SECONDS

type PowerObservation = tuple[int | str, datetime]
SLEEP_REARM_UNVERIFIED = "rearm_unverified"
SLEEP_INTENT_KEYS = ("power_intents", "scene_revisions", "scene_preset_intents")


@dataclass(slots=True)
class SleepTracking:
    """Track sleep scenary sessions per device."""

    last_scenary: str | None = None
    sleep_started_at_utc: datetime | None = None
    force_exit_requested: bool = False
    session_revision: int = 0
    observed_on: bool = False
    consecutive_off_reports: int = 0
    start_known: bool = False
    expected_minutes: int | None = None
    overdue_on_reports: int = 0
    last_confirmed_on_utc: datetime | None = None
    overdue_probe_requested: bool = False
    overdue_recorded: bool = False
    # An external or restored timer uses the first post-startup observation as
    # the *earliest safe cleanup bound*, not as proof of its original start.
    cleanup_started_at_utc: datetime | None = None
    confidence: str = "unknown"
    duration_invalidated: bool = False
    expiry_off_reports: int = 0
    seen_off_pushes: set[str] = field(default_factory=set)
    last_get_observation: int = 0
    expiry_probe_requested: bool = False
    expiry_revisions: tuple[int, int, int] | None = None

    @property
    def expiry_verified(self) -> bool:
        """Whether two reports corroborated OFF before checking current state."""
        return self.expiry_off_reports >= 2

    def invalidate_duration(self) -> None:
        """A duration change makes this session's original bounds unusable."""
        self.start_known = False
        self.duration_invalidated = True
        self.reset_expiry_votes()
        self.overdue_on_reports = 0

    def reset_expiry_votes(self) -> None:
        """Discard votes without allowing observations to be replayed."""
        self.expiry_off_reports = 0
        self.expiry_revisions = None

    def reset_expiry_session(self) -> None:
        """Start evidence collection for a different Sleep session."""
        self.reset_expiry_votes()
        self.seen_off_pushes.clear()
        self.last_get_observation = 0
        self.expiry_probe_requested = False


def sleep_cleanup_due(tracking: SleepTracking) -> datetime | None:
    """Return the session's conservative cleanup boundary, if it is usable."""
    if (
        tracking.last_scenary == SCENARY_SLEEP
        and tracking.cleanup_started_at_utc is not None
        and tracking.expected_minutes is not None
        and tracking.expected_minutes > 0
        and not tracking.duration_invalidated
    ):
        return tracking.cleanup_started_at_utc + timedelta(
            minutes=tracking.expected_minutes, seconds=SLEEP_CLEANUP_SETTLE_SECONDS
        )
    return None


def sleep_intent_revisions(
    bucket: dict[str, Any], device_id: str
) -> tuple[int, int, int]:
    """Identify the power and scene intents under which OFF was observed."""
    power, scene, preset = (
        bucket.get(key, {}).get(device_id, 0) for key in SLEEP_INTENT_KEYS
    )
    return power, scene, preset


def sleep_expiry_is_current(
    bucket: dict[str, Any],
    device_id: str,
    device: Mapping[str, Any],
    *,
    now: datetime,
    monotonic: float,
    evidence_revisions: tuple[int, int, int] | None = None,
) -> bool:
    """Validate the same live evidence for projection and dispatch."""
    tracking = bucket.get("sleep_tracking", {}).get(device_id)
    if tracking is None or not tracking.expiry_verified:
        return False
    deadline = sleep_cleanup_due(tracking)
    try:
        minutes = int(device.get("sleep_time"))
    except TypeError, ValueError:
        return False
    if (
        deadline is None
        or now < deadline
        or minutes != tracking.expected_minutes
        or tracking.expiry_revisions
        != (evidence_revisions or sleep_intent_revisions(bucket, device_id))
        or str(device.get("scenary")).strip().lower() != SCENARY_SLEEP
        or str(device.get("power")).strip().lower() not in {"0", "false", "off"}
        or device_id in bucket.get("uncertain_power", {})
    ):
        return False
    optimistic = bucket.get("optimistic", {}).get(device_id, {})
    for key, accepted in (
        ("scenary", {SCENARY_SLEEP}),
        ("power", {"0", "false", "off"}),
    ):
        overlay = optimistic.get(key, {})
        expires = overlay.get("expires")
        if (
            isinstance(expires, (int, float))
            and expires > monotonic
            and str(overlay.get("value")).strip().lower() not in accepted
        ):
            return False
    return True
