"""Airzone Cloud API client (adapted for dkn.airzonecloud.com).

Key improvements (Phase 3):
- Global cooldown after HTTP 429 to avoid hammering between requests (persists beyond a single call).
- Exponential backoff with jitter still in-place per request for 429/5xx.
- Proper asyncio timeout handling (asyncio.TimeoutError).
- PII-safe logging (never logs email/token).

Do NOT perform any blocking I/O here; all methods are async.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import aiohttp

from .const import (
    API_DEVICES,
    API_EVENTS,
    API_INSTALLATION_RELATIONS,
    API_LOGIN,
    BASE_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Retry/backoff configuration (per request)
TOTAL_TIMEOUT_SEC = 15
MAX_RETRIES = 3
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Global rate-limit backoff (persists across calls after a 429)
RL_MIN_COOLDOWN = 5.0   # seconds (initial cooldown after first 429)
RL_MAX_COOLDOWN = 60.0  # seconds (cap)


class AirzoneAPI:
    """Client to interact with the Airzone Cloud API."""

    def __init__(self, username: str, password: str, session: aiohttp.ClientSession):
        """Initialize credentials and the aiohttp session provided by HA."""
        self._username = username
        self._password = password
        self._session = session
        self.token: str = ""

        # Persistent cooldown state after rate-limit responses (429).
        self._rl_next_allowed_ts: float = 0.0
        self._rl_backoff: float = 0.0  # grows on consecutive 429s, decays implicitly with time

    # ----------------------------
    # Internal helpers
    # ----------------------------
    def _auth_params(self) -> dict[str, str]:
        """Build standard auth params for most endpoints."""
        # Never log these values; keep them internal.
        return {
            "format": "json",
            "user_email": self._username,
            "user_token": self.token,
        }

    @staticmethod
    def _redact(value: str | None) -> str:
        """Redact sensitive values for logging."""
        if not value:
            return ""
        return "***"

    async def _await_rate_limit_cooldown(self, path: str) -> None:
        """If a cooldown is active (after recent 429), wait before issuing the next request."""
        now = time.monotonic()
        if now < self._rl_next_allowed_ts:
            delay = self._rl_next_allowed_ts - now
            _LOGGER.warning(
                "Respecting API cooldown before calling %s (%.2fs remaining)...",
                path,
                delay,
            )
            await asyncio.sleep(delay)

    def _bump_rate_limit_cooldown(self) -> None:
        """Increase the persistent cooldown window due to a 429."""
        # Start at RL_MIN_COOLDOWN, then exponential up to RL_MAX_COOLDOWN.
        self._rl_backoff = (
            RL_MIN_COOLDOWN if self._rl_backoff == 0.0 else min(self._rl_backoff * 2.0, RL_MAX_COOLDOWN)
        )
        jitter = random.uniform(0.0, 0.5)
        self._rl_next_allowed_ts = time.monotonic() + self._rl_backoff + jitter

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        max_retries: int = MAX_RETRIES,
    ) -> Any:
        """Perform an HTTP request with timeout, retries, and safe logging."""
        url = f"{BASE_URL}{path}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        if json is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        if extra_headers:
            headers.update(extra_headers)

        # Avoid leaking PII in logs
        safe_params = dict(params or {})
        if "user_email" in safe_params:
            safe_params["user_email"] = self._redact(str(safe_params["user_email"]))
        if "user_token" in safe_params:
            safe_params["user_token"] = self._redact(str(safe_params["user_token"]))

        # Respect any persistent cooldown (after previous 429s)
        await self._await_rate_limit_cooldown(path)

        attempt = 0
        while True:
            attempt += 1
            try:
                timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT_SEC)
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    # If we hit a transient error/ratelimit, schedule a retry
                    if resp.status in RETRYABLE_STATUSES and attempt <= max_retries:
                        if resp.status == 429:
                            # Bump persistent cooldown so subsequent calls wait up-front
                            self._bump_rate_limit_cooldown()
                        delay = min(2 ** (attempt - 1), 10) + random.uniform(0, 0.5)
                        _LOGGER.warning(
                            "Transient API error %s on %s %s (attempt %s/%s). Retrying in %.2fs...",
                            resp.status,
                            method,
                            path,
                            attempt,
                            max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # For non-OK responses, raise to the caller
                    if resp.status >= 400:
                        if resp.status in (401, 403):
                            _LOGGER.error(
                                "Authentication/authorization error on %s %s (email=%s, token=%s).",
                                method,
                                path,
                                self._redact(self._username),
                                self._redact(self.token),
                            )
                        resp.raise_for_status()

                    # Success
                    if resp.content_type == "application/json":
                        return await resp.json()
                    # Some endpoints might return no body; still return textual content if present
                    text = await resp.text()
                    return text

            except aiohttp.ClientResponseError:
                # Non-retryable client response errors bubble up (coord handles them)
                raise
            except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError) as e:
                # Treat timeouts/connection errors as retryable
                if attempt <= max_retries:
                    delay = min(2 ** (attempt - 1), 10) + random.uniform(0, 0.5)
                    _LOGGER.warning(
                        "Network error on %s %s (attempt %s/%s): %s. Retrying in %.2fs...",
                        method,
                        path,
                        attempt,
                        max_retries,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception:
                # Unknown exceptions bubble up
                raise

    # ----------------------------
    # Public API methods
    # ----------------------------
    async def login(self) -> bool:
        """Authenticate and obtain a token via /users/sign_in (201 Created on success)."""
        try:
            data = await self._request(
                "POST",
                API_LOGIN,
                json={"email": self._username, "password": self._password},
            )
        except Exception as err:
            _LOGGER.error("Login exception: %s", err)
            return False

        # Some backends return: {"user":{"authentication_token":"..."}} on 201
        self.token = (data or {}).get("user", {}).get("authentication_token", "")
        if not self.token:
            _LOGGER.error(
                "Login failed: no token received (email=%s).",
                self._redact(self._username),
            )
            return False
        _LOGGER.debug("Login OK (token=%s).", self._redact(self.token))
        return True

    async def fetch_installations(self) -> list[dict[str, Any]]:
        """GET /installation_relations with auth query params."""
        if not self.token:
            _LOGGER.error("Cannot fetch installations without a valid token.")
            return []
        data = await self._request(
            "GET",
            API_INSTALLATION_RELATIONS,
            params=self._auth_params(),
        )
        return (data or {}).get("installation_relations", [])

    async def fetch_devices(self, installation_id: str) -> list[dict[str, Any]]:
        """GET /devices for a given installation_id."""
        params = self._auth_params()
        params["installation_id"] = installation_id
        data = await self._request(
            "GET",
            API_DEVICES,
            params=params,
        )
        return (data or {}).get("devices", [])

    async def send_event(self, payload: dict[str, Any]) -> Any:
        """POST /events with standard headers for event commands (P1..P8 etc.).

        Note:
        - This will respect any global cooldown previously set by 429 responses.
        - Per-request retries/backoff still apply.
        """
        params = self._auth_params()
        extra_headers = {"X-Requested-With": "XMLHttpRequest"}
        return await self._request(
            "POST",
            API_EVENTS,
            params=params,
            json=payload,
            extra_headers=extra_headers,
        )

    async def put_device_scenary(self, device_id: str, scenary: str) -> Any:
        """PUT /devices/{id} to change scenary: 'sleep' | 'occupied' | 'vacant'."""
        params = self._auth_params()
        # Endpoint style: /devices/<id>?format=json&user_email=...&user_token=...
        path = f"{API_DEVICES}/{device_id}"
        payload = {"device": {"scenary": scenary}}
        return await self._request("PUT", path, params=params, json=payload)
