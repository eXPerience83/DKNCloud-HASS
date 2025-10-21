"""Airzone Cloud API client (dkn.airzonecloud.com).

Notes:
- Uses HA shared aiohttp ClientSession (no I/O in entity properties).
- 30s global timeout (configurable via const), retries/backoff are applied
  for write endpoints (/events and /devices/{id}).
- Never log secrets (email/token/MAC/PIN).

This revision (P0 hotfix):
- Avoid logging ClientResponseError objects because their string representation
  may include the full request URL (including query with sensitive params).
  We now log only method, a masked path, and the HTTP status code.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from aiohttp import (
    ClientConnectorError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
)
from homeassistant.exceptions import HomeAssistantError  # For clear UI messages

from .const import (
    API_DEVICES,
    API_INSTALLATION_RELATIONS,
    BASE_URL,
    HEADERS_DEVICES,
    HEADERS_EVENTS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Backoff settings for write operations
_MAX_RETRIES = 3
_BASE_DELAY = 0.6  # seconds
_JITTER = 0.25  # added uniformly to each backoff step (0.._JITTER)


class AirzoneAPI:
    """Minimal API client for DKN Cloud."""

    def __init__(self, username: str, password: str, session: ClientSession) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._token: str | None = None
        # Cooldown after 429 to avoid hammering the backend
        self._cooldown_until: float = 0.0

    # --------------------------
    # Helpers
    # --------------------------
    def _auth_params(self) -> dict[str, str]:
        """Query params with auth info (token may be empty before login)."""
        return {"user_email": self._username, "user_token": self._token or ""}

    @staticmethod
    def _now() -> float:
        """Return event-loop monotonic time (no hass context here)."""
        return asyncio.get_running_loop().time()

    @staticmethod
    def _safe_path(path: str) -> str:
        """Return a masked path for logs (no query; only the first segment)."""
        base = (path or "").partition("?")[0].lstrip("/")
        first = base.split("/", 1)[0] if base else ""
        return f"/{first}" if first else "/"

    async def _sleep(self, seconds: float) -> None:
        """Async sleep indirection (test-friendly)."""
        if seconds > 0:
            await asyncio.sleep(seconds)

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

        timeout = ClientTimeout(total=REQUEST_TIMEOUT)
        spath = self._safe_path(path)

        try:
            async with self._session.request(
                method, url, params=params, json=json, headers=headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except ClientResponseError as cre:
            # Do NOT log the exception object (it may embed the full URL with secrets).
            # Log only method, masked path, and status code.
            _LOGGER.debug(
                "HTTP %s %s failed with status %s",
                method,
                spath,
                getattr(cre, "status", "unknown"),
            )
            raise
        except ClientConnectorError:
            # Connection errors are informative without leaking secrets.
            _LOGGER.debug("HTTP %s %s connection error", method, spath)
            raise
        except TimeoutError:
            _LOGGER.debug("HTTP %s %s timed out", method, spath)
            raise

    async def _authed_request_with_retries(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_retry_login: bool = True,
    ) -> Any:
        """Authenticated request with limited retries for 429/5xx and 1x re-login (401).

        This is intentionally used only for write endpoints to minimize risk.
        """
        attempt = 0
        while True:
            # Respect cooldown after a previous 429
            now = self._now()
            if self._cooldown_until > now:
                await self._sleep(self._cooldown_until - now)

            try:
                return await self._request(
                    method,
                    path,
                    params=params,
                    json=json,
                    extra_headers=extra_headers,
                )
            except ClientResponseError as cre:
                status = cre.status or 0

                # 401 → attempt one silent re-login then retry once
                if status == 401 and allow_retry_login and attempt == 0:
                    _LOGGER.debug("401 received; attempting one re-login before retry.")
                    await self.login()
                    attempt += 1
                    # IMPORTANT: refresh auth params so the retry does not reuse a stale token.
                    if params is not None:
                        new_auth = self._auth_params()
                        # Overwrite auth keys into caller's params while preserving non-auth keys
                        for k in ("user_email", "user_token"):
                            if k in new_auth:
                                params[k] = new_auth[k]
                    continue

                # Backoff for 429 and 5xx
                if status == 429 or 500 <= status <= 599:
                    if attempt >= _MAX_RETRIES:
                        raise

                    delay = _BASE_DELAY * (2**attempt) + random.uniform(0.0, _JITTER)

                    if status == 429:
                        # Honor Retry-After if present
                        try:
                            retry_after = cre.headers.get("Retry-After")  # type: ignore[attr-defined]
                            if retry_after:
                                delay = max(delay, float(retry_after))
                        except Exception:
                            pass
                        # Set a short cooldown to avoid hammering
                        self._cooldown_until = max(
                            self._cooldown_until, now + min(delay, 10.0)
                        )

                    _LOGGER.debug(
                        "Retrying %s %s after %s due to HTTP %s (attempt %d/%d)",
                        method,
                        self._safe_path(path),
                        round(delay, 2),
                        status,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    attempt += 1
                    await self._sleep(delay)
                    continue

                # Other HTTP errors → propagate
                raise
            except (TimeoutError, ClientConnectorError):
                # Network issues → propagate (do not loop indefinitely)
                raise

    # --------------------------
    # Public API
    # --------------------------
    async def login(self) -> bool:
        """Login and store authentication token.

        Returns:
            True if credentials were accepted and a token was received.
            False only when the server explicitly rejects with 401.
        Raises:
            TimeoutError, ClientConnectorError for network issues.
            ClientResponseError for non-401 HTTP errors (e.g. 5xx).
        """
        data = {"email": self._username, "password": self._password}
        try:
            # Minimal headers (UA only) are enough here.
            resp = await self._request("POST", "users/sign_in", json=data)
        except ClientResponseError as cre:
            if cre.status == 401:
                return False  # invalid credentials
            raise
        # Network errors (TimeoutError, ClientConnectorError) bubble up.

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
            return resp.get("installation_relations")
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
            return resp.get("devices")
        if isinstance(resp, list):
            return resp
        return None

    async def send_event(self, payload: dict[str, Any]) -> Any:
        """POST to /events (realtime control) with JSON/XHR headers + retries.

        English:
        - If the backend returns 422, we raise a HomeAssistantError with a clear
          user-facing message in Spanish as requested:
          "DKN WServer sin conexión (422)".
        """
        params = self._auth_params()
        try:
            return await self._authed_request_with_retries(
                "POST",
                "events/",
                params=params,
                json=payload,
                extra_headers=HEADERS_EVENTS,
                allow_retry_login=True,
            )
        except ClientResponseError as cre:
            if cre.status == 422:
                # Raise a HA-friendly error (no PII) with the exact message requested.
                raise HomeAssistantError("DKN WServer sin conexión (422)") from cre
            raise

    # ---------- Generic PUT helpers for /devices/<id> ----------
    async def put_device_fields(self, device_id: str, payload: dict[str, Any]) -> Any:
        """PUT /devices/{id} with provided payload (retries for 429/5xx, 1x re-login)."""
        params = self._auth_params() | {"format": "json"}
        path = f"{API_DEVICES}/{device_id}"
        return await self._authed_request_with_retries(
            "PUT", path, params=params, json=payload, allow_retry_login=True
        )

    async def put_device_scenary(self, device_id: str, scenary: str) -> Any:
        """Change scenary: 'occupied' | 'vacant' | 'sleep'."""
        return await self.put_device_fields(device_id, {"device": {"scenary": scenary}})

    async def put_device_sleep_time(self, device_id: str, minutes: int) -> Any:
        """Change sleep_time (30..120, step 10)."""
        return await self.put_device_fields(device_id, {"sleep_time": int(minutes)})
