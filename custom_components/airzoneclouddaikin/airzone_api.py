"""Airzone Cloud API client (dkn.airzonecloud.com).

Notes:
- Uses HA shared aiohttp ClientSession (no I/O in entity properties).
- 15s global timeout, retries/backoff are handled at higher levels (coordinator).
- Never log secrets (email/token/MAC/PIN).
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientResponseError, ClientSession, ClientTimeout

from .const import (
    API_DEVICES,
    API_INSTALLATION_RELATIONS,
    BASE_URL,
    USER_AGENT,
    HEADERS_DEVICES,
    HEADERS_EVENTS,
)

_LOGGER = logging.getLogger(__name__)


class AirzoneAPI:
    """Minimal API client for DKN Cloud."""

    def __init__(self, username: str, password: str, session: ClientSession) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._token: str | None = None

    # --------------------------
    # Helpers
    # --------------------------
    def _auth_params(self) -> dict[str, str]:
        """Query params with auth info."""
        return {"user_email": self._username, "user_token": self._token or ""}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """HTTP request helper.

        English:
        - Default headers are minimal (only User-Agent).
        - Endpoint-specific headers can be provided via 'extra_headers'
          to match the project's cURL behaviour (e.g., /events JSON/XHR).
        """
        url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        headers = {"User-Agent": USER_AGENT}
        if extra_headers:
            headers.update(extra_headers)

        timeout = ClientTimeout(total=15)

        try:
            async with self._session.request(
                method, url, params=params, json=json, headers=headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except ClientResponseError as cre:
            # Mask sensitive info
            _LOGGER.debug("HTTP %s %s failed: %s", method, path, cre)
            raise
        except TimeoutError:
            _LOGGER.debug("HTTP %s %s timed out", method, path)
            raise

    # --------------------------
    # Public API
    # --------------------------
    async def login(self) -> bool:
        """Login and store authentication token."""
        data = {"email": self._username, "password": self._password}
        try:
            # Minimal headers (UA only) are enough here.
            resp = await self._request("POST", "users/sign_in", json=data)
        except Exception:
            return False

        # Accept both shapes:
        #   {"user": {"authentication_token": "..."}}
        #   {"authentication_token": "..."}
        token = (resp or {}).get("user", {}).get("authentication_token") or (
            resp or {}
        ).get("authentication_token")
        if not token:
            _LOGGER.debug("Login response did not include expected token field.")
            return False

        self._token = str(token)
        return True

    async def sign_out(self) -> None:
        """Optional sign out endpoint."""
        try:
            await self._request("DELETE", "users/sign_out", params=self._auth_params())
        except Exception:
            # non-fatal
            return

    async def fetch_installations(self) -> list[dict[str, Any]] | None:
        """GET installation relations."""
        params = self._auth_params() | {"format": "json"}
        resp = await self._request("GET", API_INSTALLATION_RELATIONS, params=params)
        # Normalize: return the list directly if wrapped
        if isinstance(resp, dict) and "installation_relations" in resp:
            return resp.get("installation_relations")  # type: ignore[return-value]
        if isinstance(resp, list):
            return resp
        return None

    async def fetch_devices(self, installation_id: Any) -> list[dict[str, Any]] | None:
        """GET devices for an installation (browser-like UA only)."""
        params = self._auth_params() | {
            "format": "json",
            "installation_id": str(installation_id),
        }
        resp = await self._request(
            "GET", API_DEVICES, params=params, extra_headers=HEADERS_DEVICES
        )
        if isinstance(resp, dict) and "devices" in resp:
            return resp.get("devices")  # type: ignore[return-value]
        if isinstance(resp, list):
            return resp
        return None

    async def send_event(self, payload: dict[str, Any]) -> Any:
        """POST to /events (realtime control) with JSON/XHR headers."""
        params = self._auth_params()
        return await self._request(
            "POST", "events/", params=params, json=payload, extra_headers=HEADERS_EVENTS
        )

    # ---------- Generic PUT helpers for /devices/<id> ----------
    async def put_device_fields(self, device_id: str, payload: dict[str, Any]) -> Any:
        """PUT /devices/{id} with provided payload (UA-only headers are sufficient)."""
        params = self._auth_params() | {"format": "json"}
        path = f"{API_DEVICES}/{device_id}"
        return await self._request("PUT", path, params=params, json=payload)

    async def put_device_scenary(self, device_id: str, scenary: str) -> Any:
        """Change scenary: 'occupied' | 'vacant' | 'sleep'."""
        return await self.put_device_fields(device_id, {"device": {"scenary": scenary}})

    async def put_device_sleep_time(self, device_id: str, minutes: int) -> Any:
        """Change sleep_time (30..120, step 10)."""
        return await self.put_device_fields(device_id, {"sleep_time": int(minutes)})
