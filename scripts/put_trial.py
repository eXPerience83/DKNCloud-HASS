"""Two exact PUT attempts for the isolated Pusher experiment; never Sleep."""

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal

from yarl import URL

from scripts.lab_policy import ReadOnlyPolicy
from scripts.setpoint_trial import BoundedSetpointPolicy, _whole


@dataclass(frozen=True)
class PutPlan:
    device_id: str
    installation_id: str
    mode: str
    field: str
    original: str | int
    modified: str | int
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    option: str = "PUT"
    power: str = "1"

    @classmethod
    def from_device(cls, device, *, device_id, installation_id, field):
        if (
            str(device.get("id")) != device_id
            or str(device.get("power")) not in {"0", "1"}
            or device.get("scenary") != "occupied"
            or device.get("effective_scenary") not in {None, "occupied"}
            or str(device.get("mode")) not in {"1", "2", "3", "5"}
        ):
            raise ValueError(
                "PUT baseline requires a scoped Home device with known power"
            )
        if field == "scenary":
            return cls(
                device_id,
                installation_id,
                str(device["mode"]),
                field,
                "occupied",
                "vacant",
                power=str(device["power"]),
            )
        if field != "min_temp_unoccupied":
            raise ValueError("PUT field is not authorized")
        original = _whole(device.get(field))
        if not 12 <= original <= 22:
            raise ValueError(
                "Unoccupied heat minimum outside the documented 12..22 range"
            )
        return cls(
            device_id,
            installation_id,
            str(device["mode"]),
            field,
            original,
            original + 1 if original < 22 else original - 1,
            Decimal(12),
            Decimal(22),
            power=str(device["power"]),
        )

    def check_device(self, device, *, restoring):
        expected = self.modified if restoring else self.original
        scene = expected if self.field == "scenary" else "occupied"
        value = device.get(self.field)
        if self.field != "scenary":
            value = _whole(value)
        if (
            str(device.get("id")) != self.device_id
            or str(device.get("power")) != self.power
            or str(device.get("mode")) != self.mode
            or device.get("scenary") != scene
            or device.get("effective_scenary") not in {None, scene}
            or value != expected
        ):
            raise PermissionError("PUT state changed since the approved plan")

    def exact_payload(self, *, restoring):
        value = self.original if restoring else self.modified
        return (
            {"device": {"scenary": value}}
            if self.field == "scenary"
            else {self.field: value}
        )


class BoundedPutPolicy(BoundedSetpointPolicy):
    """Retain journal, attempt consumption and revocation; allow only the frozen PUT."""

    @staticmethod
    def _is_control(method, url, data):
        return method == "PUT"

    def validate(self, method, url, *, params=None, json=None, data=None):
        if method != "PUT":
            return ReadOnlyPolicy.validate(
                self, method, url, params=params, json=json, data=data
            )
        target = URL(url)
        if (
            target.origin() != URL("https://dkn.airzonecloud.com")
            or target.query
            or target.fragment
            or target.user
            or target.path != f"/devices/{self.plan.device_id}"
            or data is not None
            or type(params) is not dict
            or set(params) != {"user_email", "user_token", "format"}
            or params["format"] != "json"
            or not all(isinstance(x, str) and x for x in params.values())
            or self._revoked
            or time.monotonic() >= self.deadline
            or self._next_slot not in {1, 2}
            or (self._next_slot == 1 and not self._armed)
            or (self._next_slot == 2 and not self._restoration_approved)
            or json != self.plan.exact_payload(restoring=self._next_slot == 2)
        ):
            raise PermissionError("PUT differs from the exact session authorization")
        self.plan.check_device(self._read_device(), restoring=self._next_slot == 2)

    def reserve_transport(self, method, url, *, params=None, json=None, data=None):
        self.validate(method, url, params=params, json=json, data=data)
        ReadOnlyPolicy.reserve(self, method, url)
        if method == "PUT":
            stage = self._next_slot
            self._armed = self._restoration_approved = False
            self._next_slot = stage + 1
            self.journal.consume(
                stage,
                option="PUT",
                value=str(self.plan.original if stage == 2 else self.plan.modified),
            )

    def arm_restoration(
        self, *, confirmed_by_push=False, confirmed_by_snapshot=False, owner_approved
    ):
        return super().arm_restoration(
            confirmed_by_push=confirmed_by_push or confirmed_by_snapshot,
            owner_approved=owner_approved,
        )

    @staticmethod
    def freeze_control_kwargs(method, url, kwargs):
        return deepcopy(kwargs) if method == "PUT" else kwargs


class ReadWindow:
    """Persistent per-operation gates, acquired atomically at transport admission."""

    def __init__(self):
        self._events = {kind: asyncio.Event() for kind in ("info", "get")}
        self._idle = {kind: asyncio.Event() for kind in self._events}
        self._active = dict.fromkeys(self._events, 0)
        self._permits = dict.fromkeys(self._events, None)
        self.resume()
        for event in self._idle.values():
            event.set()

    async def wait(self, operation):
        await self._events[operation].wait()

    def acquire(self, operation):
        """No await between this check and actual transport delegation."""
        if not self._events[operation].is_set():
            return False
        remaining = self._permits[operation]
        if remaining is not None:
            self._permits[operation] = remaining - 1
            if remaining == 1:
                self._events[operation].clear()
        self._active[operation] += 1
        self._idle[operation].clear()
        return True

    def leave(self, operation):
        self._active[operation] -= 1
        if self._active[operation] == 0:
            self._idle[operation].set()

    async def pause(self, *operations):
        kinds = operations or tuple(self._events)
        for kind in kinds:
            self._permits[kind] = 0
            self._events[kind].clear()
        for kind in kinds:
            await self._idle[kind].wait()

    def resume(self, *operations):
        for kind in operations or tuple(self._events):
            self._permits[kind] = None
            self._events[kind].set()

    def allow_once(self, operation):
        if self._events[operation].is_set() or self._active[operation]:
            raise RuntimeError("A single read requires a drained, paused gate")
        self._permits[operation] = 1
        self._events[operation].set()

    def paused(self, operation):
        return not self._events[operation].is_set()
