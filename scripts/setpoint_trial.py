"""Offline-first, narrowly scoped control authorization for a future DKN LAB trial.

This module is not a live launcher. The current real-destination switch remains
disabled. A separate, reviewed runner must verify and freeze owner approval
before constructing this policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from yarl import URL

from scripts.lab_policy import ReadOnlyPolicy


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing numeric trial value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Invalid numeric trial value") from error
    if not result.is_finite():
        raise ValueError("Nonfinite trial value")
    return result


def _whole(value: object) -> int:
    parsed = _decimal(value)
    if parsed != parsed.to_integral_value():
        raise ValueError("The DKN setpoint must be an exact whole Celsius degree")
    return int(parsed)


@dataclass(frozen=True, slots=True)
class SetpointPlan:
    """Freeze one exact, reversible P7/P8 plan from verified raw telemetry."""

    device_id: str
    installation_id: str
    mode: str
    field: str
    option: str
    original: int
    modified: int
    minimum: Decimal
    maximum: Decimal

    @classmethod
    def from_device(
        cls, device: dict, *, device_id: str, installation_id: str
    ) -> SetpointPlan:
        if not device_id or not installation_id or device.get("id") != device_id:
            raise ValueError("Trial device scope is not confirmed")
        if str(device.get("power")) != "1":
            raise ValueError("The device must already be powered ON")
        if str(device.get("scenary") or "").strip().lower() != "occupied":
            raise ValueError("The device must already be in raw Home")
        mode = str(device.get("mode"))
        if mode not in {"1", "2"}:
            raise ValueError("Trial only permits actual COOL or HEAT")
        cold = mode == "1"
        field = "cold_consign" if cold else "heat_consign"
        lower_field = "min_limit_cold" if cold else "min_limit_heat"
        upper_field = "max_limit_cold" if cold else "max_limit_heat"
        lower = _decimal(device.get(lower_field))
        upper = _decimal(device.get(upper_field))
        original = _whole(device.get(field))
        if lower > upper or not lower <= original <= upper:
            raise ValueError("Unusable live device setpoint limits")
        if original + 1 <= upper:
            modified = original + 1
        elif original - 1 >= lower:
            modified = original - 1
        else:
            raise ValueError("No one-degree change is within the device limits")
        return cls(
            device_id=device_id,
            installation_id=installation_id,
            mode=mode,
            field=field,
            option="P7" if cold else "P8",
            original=original,
            modified=modified,
            minimum=lower,
            maximum=upper,
        )

    def check_device(self, device: dict, *, restoring: bool) -> None:
        """Fail closed after any competing mode, scene, limit or setpoint update."""
        other = self.from_device(
            device, device_id=self.device_id, installation_id=self.installation_id
        )
        if (
            other.mode != self.mode
            or other.field != self.field
            or other.minimum != self.minimum
            or other.maximum != self.maximum
            or _whole(device[self.field])
            != (self.modified if restoring else self.original)
        ):
            raise PermissionError("Trial telemetry changed since approval")
        if not restoring and other.modified != self.modified:
            raise PermissionError("Trial change no longer matches")

    def exact_payload(self, *, restoring: bool) -> dict:
        value = self.original if restoring else self.modified
        return {
            "event": {
                "cgi": "modmaquina",
                "device_id": self.device_id,
                "option": self.option,
                "value": f"{value}.0",
            }
        }


class SingleUseTrialJournal:
    """Private, durable, exclusive nonce claim and fsynced attempt accounting.

    A failed/cancelled attempt never releases its slot; restarting a nonce is
    forbidden even after a crash. Persist this directory outside HA's disposable
    configuration and outside the repository.
    """

    def __init__(self, directory: Path, *, nonce: str, plan: SetpointPlan):
        if len(nonce) < 32 or len(nonce) > 256:
            raise PermissionError("Invalid trial nonce")
        directory = Path(directory)
        st = directory.lstat()
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
        ):
            raise PermissionError("Trial journal directory must be private (0700)")
        name = hashlib.sha256(nonce.encode("utf-8")).hexdigest() + ".jsonl"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self.path = directory / name
        self._fd = os.open(self.path, flags, 0o600)
        try:
            self._append(
                {
                    "kind": "claim",
                    "device": plan.device_id,
                    "installation": plan.installation_id,
                    "mode": plan.mode,
                    "option": plan.option,
                    "original": plan.original,
                    "modified": plan.modified,
                }
            )
            dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except BaseException:
            self.close()
            # Keep an incomplete claim rather than allowing nonce reuse.
            raise

    def _append(self, record: dict) -> None:
        blob = (
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        written = os.write(self._fd, blob)
        if written != len(blob):
            raise OSError("Incomplete journal append")
        os.fsync(self._fd)

    def consume(self, slot: int, *, option: str, value: str) -> None:
        self._append(
            {
                "kind": "attempt",
                "slot": slot,
                "option": option,
                "value": value,
                "monotonic_ns": time.monotonic_ns(),
            }
        )

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            os.close(self._fd)
            self._fd = None


class BoundedSetpointPolicy(ReadOnlyPolicy):
    """One private device, two exact attempts, no generic writable capability."""

    def __init__(
        self,
        plan: SetpointPlan,
        journal: SingleUseTrialJournal,
        read_device,
        app_key: str,
        *,
        deadline: float,
    ):
        super().__init__(
            {plan.device_id}, {plan.installation_id}, app_key, enabled=True
        )
        self.deadline = min(self.deadline, deadline)
        self.plan = plan
        self.journal = journal
        self._read_device = read_device
        self._next_slot = 1
        self._armed = False
        self._restoration_approved = False
        self._first_accepted = False
        self._revoked = False

    @staticmethod
    def _is_control(method: str, url: str, data: object) -> bool:
        return (
            method == "POST"
            and URL(url).path == "/events"
            and type(data) is dict
            and type(data.get("event")) is dict
            and data["event"].get("cgi") == "modmaquina"
        )

    def arm_change(self) -> None:
        if self._next_slot != 1 or self._armed:
            raise PermissionError("Trial change already armed or consumed")
        self.plan.check_device(self._read_device(), restoring=False)
        self._armed = True

    def arm_restoration(self, *, confirmed_by_push: bool, owner_approved: bool) -> None:
        if (
            self._next_slot != 2
            or self._restoration_approved
            or not self._first_accepted
            or self._revoked
        ):
            raise PermissionError("Trial restoration unavailable")
        if not (confirmed_by_push and owner_approved):
            raise PermissionError("Restoration requires telemetry and owner approval")
        self.plan.check_device(self._read_device(), restoring=True)
        self._restoration_approved = True

    def validate(self, method, url, *, params=None, json=None, data=None):
        if not self._is_control(method, url, json):
            return super().validate(method, url, params=params, json=json, data=data)
        # All common origin/URL/header checks still apply through the parent
        # protected session. Reject additional shapes, scopes and parameters.
        target = URL(url)
        if (
            target.origin() != URL("https://dkn.airzonecloud.com")
            or target.query
            or target.fragment
            or target.user
            or data is not None
            or type(params) is not dict
            or set(params) != {"user_email", "user_token"}
            or not all(isinstance(x, str) and x for x in params.values())
            or type(json) is not dict
            or set(json) != {"event"}
        ):
            raise PermissionError("Trial control envelope denied")
        stage = self._next_slot
        if stage == 1 and not self._armed:
            raise PermissionError("Trial change not armed")
        if stage == 2 and not self._restoration_approved:
            raise PermissionError("Trial restoration not armed")
        if stage not in {1, 2} or self._revoked or time.monotonic() >= self.deadline:
            raise PermissionError("Trial control exhausted")
        expected = self.plan.exact_payload(restoring=stage == 2)
        if json != expected or set(json["event"]) != {
            "cgi",
            "device_id",
            "option",
            "value",
        }:
            raise PermissionError("Trial control differs from frozen approval")
        self.plan.check_device(self._read_device(), restoring=stage == 2)

    def reserve_transport(
        self, method, url, *, params=None, json=None, data=None
    ) -> None:
        """Revalidate after supervisor budget wait; consume BEFORE network I/O."""
        self.validate(method, url, params=params, json=json, data=data)
        super().reserve(method, url)
        if not self._is_control(method, url, json):
            return
        stage = self._next_slot
        self._armed = False
        self._restoration_approved = False
        self._next_slot = 3 if stage == 2 else 2
        self.journal.consume(
            stage,
            option=self.plan.option,
            value=self.plan.exact_payload(restoring=stage == 2)["event"]["value"],
        )

    def control_result(self, *, accepted: bool) -> None:
        """Record the response once; failed/ambiguous control revokes slot two."""
        if not accepted:
            self._revoked = True
        elif self._next_slot == 2:
            self._first_accepted = True

    @staticmethod
    def freeze_control_kwargs(method: str, url: str, kwargs: dict) -> dict:
        """Prevent queued request mutations from changing the on-wire command."""
        data = kwargs.get("json")
        if BoundedSetpointPolicy._is_control(method, url, data):
            result = dict(kwargs)
            result["json"] = deepcopy(data)
            result["params"] = deepcopy(kwargs.get("params"))
            return result
        return kwargs
