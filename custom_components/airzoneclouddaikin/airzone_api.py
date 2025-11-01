"""Airzone Cloud API client (dkn.airzonecloud.com).

Auth & resilience:
- No "silent re-login": HTTP 401 is surfaced so the coordinator opens a reauth flow.
- Backoff with jitter is applied to transient 429/5xx responses (GET and writes).
- Logging remains secret-safe (never logs full URLs with query params).

P3:
- Provide a safe __repr__ that never leaks the token and masks the email,
  protecting against accidental repr() in logs or traces.

P4-A/B:
- Remove redundant per-endpoint User-Agent headers. _request() is the single
  source of truth for the UA. GET endpoints no longer pass extra headers.
- Replace hard-coded paths with API_* constants for coherence (no runtime change).

Timeouts:
- Catch only built-in TimeoutError (asyncio.TimeoutError is an alias on 3.11+).
- Do ONE gentle retry on timeouts.

Important:
- 401 is never retried here; it must bubble up to the coordinator.
- Legacy "scenary" helpers have been **removed**. Use put_device_fields(...) instead.
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
from homeassistant.exceptions import HomeAssistantError

from .const import (
    API_DEVICES,
    API_EVENTS,
    API_INSTALLATION_RELATIONS,
    API_LOGIN,
    API_LOGOUT,
    BASE_URL,
    DOMAIN,
    HEADERS_EVENTS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_DELAY = 0.6
_JITTER = 0.25


class AirzoneAPI:
    """Minimal API client for DKN Cloud."""

    def __init__(
        self,
        username: str,
        session: ClientSession,
        *,
        password: str | None = None,
        token: str | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._session = session
        self._token: str | None = token
        self._cooldown_until: float = 0.0

    def __repr__(self) -> str:
        """Return a safe representation that never leaks secrets."""
        u = str(self._username or "")
        masked_u = "***"
        if "@" in u and u:
            masked_u = f"{u[0]}***@***"
        elif u:
            masked_u = f"{u[0]}***"
        token_state = "set" if bool(self._token) else "none"
        return f"AirzoneAPI(username='{masked_u}', token={token_state})"

    # --------------------------
    # Public props
    # --------------------------
    @property
    def token(self) -> str | None:
        """Return current auth token."""
        return self._token

    def set_token(self, token: str | None) -> None:
        """Update auth token for subsequent requests."""
        self._token = token

    @property
    def password(self) -> str | None:
        """Expose the current password in memory (if any)."""
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        """Update the stored password (used for hygiene in config flow)."""
        self._password = value

    def clear_password(self) -> None:
        """Explicit helper to purge the password from memory."""
        self._password = None

    # --------------------------
    # Helpers
    # --------------------------
    def _auth_params(self) -> dict[str, str]:
        """Query params with auth info (token may be empty before login)."""
        return {"user_email": self._username, "user_token": self._token or ""}

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @staticmethod
    def _safe_path(path: str) -> str:
        """Return a safe path fragment for logs (no query string, no secrets)."""
        base = (path or "").partition("?")[0].lstrip("/")
        first = base.split("/", 1)[0] if base else ""
        return f"/{first}" if first else "/"

    async def _sleep(self, seconds: float) -> None:
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
        """HTTP request helper (never logs full URLs with secrets)."""
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
            _LOGGER.debug(
                "HTTP %s %s failed with status %s",
                method,
                spath,
                getattr(cre, "status", "unknown"),
            )
            raise
        except ClientConnectorError:
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
    ) -> Any:
        """Authenticated request with limited retries for 429/5xx + ONE retry on timeout.

        English:
        - 401 is *not* retried here; it is propagated so the coordinator opens reauth.
        """
        attempt = 0
        while True:
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

                # 401 → let coordinator handle reauth
                if status == 401:
                    raise

                # Backoff for 429 and 5xx (transient errors)
                if status == 429 or 500 <= status <= 599:
                    if attempt >= _MAX_RETRIES:
                        raise
                    delay = _BASE_DELAY * (2**attempt) + random.uniform(0.0, _JITTER)
                    if status == 429:
                        try:
                            retry_after = cre.headers.get("Retry-After")  # type: ignore[attr-defined]
                            if retry_after:
                                delay = max(delay, float(retry_after))
                        except Exception:  # noqa: BLE001
                            pass
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

            except TimeoutError:
                # ONE gentle retry on timeout with short backoff
                if attempt >= 1:
                    raise
                delay = 0.4 * (2**attempt) + random.uniform(0.0, _JITTER)
                _LOGGER.debug(
                    "Retrying %s %s after timeout (attempt %d): %ss",
                    method,
                    self._safe_path(path),
                    attempt + 1,
                    round(delay, 2),
                )
                attempt += 1
                await self._sleep(delay)
                continue

            except ClientConnectorError:
                # Connection issues → propagate (HA will surface the error)
                raise

    # --------------------------
    # Public API
    # --------------------------
    async def login(self) -> bool:
        """Login and store authentication token (used by config/reauth flows)."""
        if not self._password:
            _LOGGER.debug("login() called without password; returning False.")
            return False

        data = {"email": self._username, "password": self._password}
        try:
            resp = await self._request("POST", API_LOGIN, json=data)
        except ClientResponseError as cre:
            if cre.status == 401:
                return False
            raise

        token = (resp or {}).get("user", {}).get("authentication_token") or (
            resp or {}
        ).get("authentication_token")
        if not token:
            _LOGGER.debug("Login response did not include expected token field.")
            return False

        self._token = str(token)
        return True

    async def sign_out(self) -> None:
        """Optional sign out endpoint (best-effort)."""
        try:
            await self._request("DELETE", API_LOGOUT, params=self._auth_params())
        except Exception:  # noqa: BLE001
            return

    async def fetch_installations(self) -> list[dict[str, Any]] | None:
        """GET /installation_relations with backoff for 429/5xx (401 bubbles up)."""
        params = self._auth_params() | {"format": "json"}
        resp = await self._authed_request_with_retries(
            "GET",
            API_INSTALLATION_RELATIONS,
            params=params,
        )
        if isinstance(resp, dict) and "installation_relations" in resp:
            return resp.get("installation_relations")
        if isinstance(resp, list):
            return resp
        return None

    async def fetch_devices(self, installation_id: Any) -> list[dict[str, Any]] | None:
        """GET /devices with backoff for 429/5xx (401 bubbles up)."""
        params = self._auth_params() | {
            "format": "json",
            "installation_id": str(installation_id),
        }
        resp = await self._authed_request_with_retries(
            "GET",
            API_DEVICES,
            params=params,
        )
        if isinstance(resp, dict) and "devices" in resp:
            return resp.get("devices")
        if isinstance(resp, list):
            return resp
        return None

    async def send_event(self, payload: dict[str, Any]) -> Any:
        """POST to /events (realtime control) with JSON/XHR headers + retries."""
        params = self._auth_params()
        try:
            return await self._authed_request_with_retries(
                "POST",
                API_EVENTS,
                params=params,
                json=payload,
                extra_headers=HEADERS_EVENTS,
            )
        except ClientResponseError as cre:
            if cre.status == 422:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="error_wserver_not_connected",
                ) from cre
            raise

    async def put_device_fields(self, device_id: str, payload: dict[str, Any]) -> Any:
        """PUT /devices/{id} with provided payload (retries for 429/5xx).

        Canonical method for *all* device field writes (including what used to be "scenary").
        Example:
            await api.put_device_fields("123", {"device": {"preset": "sleep"}})
        """
        params = self._auth_params() | {"format": "json"}
        path = f"{API_DEVICES}/{device_id}"
        return await self._authed_request_with_retries(
            "PUT", path, params=params, json=payload
        )
