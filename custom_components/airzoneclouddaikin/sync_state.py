"""Transport-independent state reconciliation for cloud synchronization."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class SyncState:
    """Merge snapshots without overwriting fields received during a GET."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.generation = 0
        self.revision = 0
        self._fields: dict[str, dict[str, int]] = {}

    def checkpoint(self) -> tuple[int, int]:
        """Capture a GET's generation and revision before starting I/O."""
        return self.generation, self.revision

    def invalidate(self) -> None:
        """Reject results from an obsolete connection or entry lifetime."""
        self.generation += 1

    def event(self, device_id: str, patch: dict[str, Any], generation: int) -> bool:
        """Apply a patch for a known device; absent/null fields preserve state."""
        if generation != self.generation or device_id not in self.data:
            return False
        if "id" in patch and str(patch["id"]) != device_id:
            return False
        self.revision += 1
        state = deepcopy(self.data[device_id])
        fields = self._fields.setdefault(device_id, {})
        for key, value in patch.items():
            if value is not None:
                state[key] = deepcopy(value)
                fields[key] = self.revision
        changed = state != self.data[device_id]
        self.data[device_id] = state
        return changed

    def snapshot(
        self, devices: list[dict[str, Any]], checkpoint: tuple[int, int]
    ) -> bool:
        """Merge a successful GET; deletion requires separate discovery."""
        generation, revision = checkpoint
        if generation != self.generation:
            return False
        before = deepcopy(self.data)
        for device in devices:
            device_id = str(device.get("id") or "")
            if not device_id:
                continue
            state = deepcopy(self.data.get(device_id, {}))
            fields = self._fields.get(device_id, {})
            for key, value in device.items():
                if value is not None and fields.get(key, 0) <= revision:
                    state[key] = deepcopy(value)
            self.data[device_id] = state
        return self.data != before
