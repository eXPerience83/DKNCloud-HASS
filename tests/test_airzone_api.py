"""Retry behavior tests for the Airzone API HTTP client."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ------------------------------ aiohttp stubs ------------------------------
aiohttp_stub = types.ModuleType("aiohttp")


class _ClientResponseError(Exception):
    def __init__(
        self, status: int | None = None, headers: dict[str, str] | None = None
    ):
        super().__init__(status)
        self.status = status
        self.headers = headers or {}


class _ClientConnectorError(Exception): ...


class _ClientSession:
    def __init__(self) -> None:  # pragma: no cover - not used directly
        ...


class _ClientTimeout:
    def __init__(self, total: float | None = None) -> None:  # pragma: no cover
        self.total = total


aiohttp_stub.ClientResponseError = _ClientResponseError
aiohttp_stub.ClientConnectorError = _ClientConnectorError
aiohttp_stub.ClientSession = _ClientSession
aiohttp_stub.ClientTimeout = _ClientTimeout
sys.modules.setdefault("aiohttp", aiohttp_stub)


# --------------------- Home Assistant exceptions stub ----------------------
ha_exceptions = types.ModuleType("homeassistant.exceptions")


class _HomeAssistantError(Exception): ...


y_stub = types.ModuleType("homeassistant")
sys.modules.setdefault("homeassistant", y_stub)
ha_exceptions.HomeAssistantError = _HomeAssistantError
sys.modules.setdefault("homeassistant.exceptions", ha_exceptions)


# --------------------- Custom components package hook ----------------------
custom_components_module = types.ModuleType("custom_components")
custom_components_module.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components_module)

airzone_package = types.ModuleType("custom_components.airzoneclouddaikin")
airzone_package.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]
sys.modules.setdefault("custom_components.airzoneclouddaikin", airzone_package)


spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.airzone_api",
    ROOT / "custom_components" / "airzoneclouddaikin" / "airzone_api.py",
)
airzone_api = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = airzone_api
spec.loader.exec_module(airzone_api)
AirzoneAPI = airzone_api.AirzoneAPI
_ClientResponseError = airzone_api.ClientResponseError


class AirzoneAPITestStub(AirzoneAPI):
    """Override timing and HTTP entrypoints for deterministic testing."""

    def __init__(self, responses: list[Any]):
        super().__init__(username="user@example.com", session=_ClientSession())
        self._time = 0.0
        self._sleeps: list[float] = []
        self._responses = list(responses)

    @property
    def sleeps(self) -> list[float]:
        return self._sleeps

    def _now(self) -> float:  # type: ignore[override]
        return self._time

    async def _sleep(self, seconds: float) -> None:  # type: ignore[override]
        self._sleeps.append(seconds)
        self._time += seconds

    async def _request(self, *_args: Any, **_kwargs: Any) -> Any:  # type: ignore[override]
        if not self._responses:
            raise AssertionError("Unexpected extra request")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_retry_after_sets_cooldown_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 with Retry-After should honor the header and set cooldown."""

    monkeypatch.setattr(airzone_api.random, "uniform", lambda *_args: 0.0)
    retry_error = _ClientResponseError(status=429, headers={"Retry-After": "2"})
    api = AirzoneAPITestStub([retry_error, {"ok": True}])

    result = asyncio.run(api._authed_request_with_retries("GET", "/foo"))

    assert result == {"ok": True}
    assert api.sleeps == [2.0]
    assert api._cooldown_until == pytest.approx(2.0)


def test_500_retries_use_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx responses without Retry-After should back off exponentially then raise."""

    monkeypatch.setattr(airzone_api.random, "uniform", lambda *_args: 0.0)
    errors = [_ClientResponseError(status=500) for _ in range(4)]
    api = AirzoneAPITestStub(errors)

    with pytest.raises(_ClientResponseError):
        asyncio.run(api._authed_request_with_retries("GET", "/bar"))

    assert api.sleeps == pytest.approx([0.6, 1.2, 2.4])
    assert api._cooldown_until == 0.0
