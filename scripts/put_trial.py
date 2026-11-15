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

    @classmethod
    def from_device(cls, device, *, device_id, installation_id, field):
        if (
            str(device.get("id")) != device_id
            or str(device.get("power")) != "1"
            or device.get("scenary") != "occupied"
            or device.get("effective_scenary") not in {None, "occupied"}
            or str(device.get("mode")) not in {"1", "2", "3", "5"}
        ):
            raise ValueError("PUT baseline requires a scoped, powered Home device")
        if field == "scenary":
            return cls(
                device_id,
                installation_id,
                str(device["mode"]),
                field,
                "occupied",
                "vacant",
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
        )

    def check_device(self, device, *, restoring):
        expected = self.modified if restoring else self.original
        scene = expected if self.field == "scenary" else "occupied"
        value = device.get(self.field)
        if self.field != "scenary":
            value = _whole(value)
        if (
            str(device.get("id")) != self.device_id
            or str(device.get("power")) != "1"
            or str(device.get("mode")) != self.mode
            or device.get("scenary") != scene
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
    """LAB-only pause: drain existing INFO/GET, defer new ones, keep WSS alive."""

    def __init__(self):
        self.open = asyncio.Event()
        self.open.set()
        self.idle = asyncio.Event()
        self.idle.set()
        self.active = 0

    async def enter(self):
        while True:
            await self.open.wait()
            if self.open.is_set():
                break
        self.active += 1
        self.idle.clear()

    def leave(self):
        self.active -= 1
        if self.active == 0:
            self.idle.set()

    async def pause(self):
        self.open.clear()
        await self.idle.wait()

    def resume(self):
        self.open.set()
