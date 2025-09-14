"""Airzone Cloud API client (adapted for dkn.airzonecloud.com).

Key improvements:
- Per-request timeout (15s) using aiohttp ClientTimeout.
- Exponential backoff with jitter for 429/5xx, explicit handling for 401/403.
- Centralized _request() helper to ensure consistent headers/params and error handling.
- PII redaction in logs (email/token never printed).
- Public helper put_device_scenary() prepared for future select.scenary entity.

Do NOT perform any blocking I/O here; all methods are async.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

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

# Retry/backoff configuration
TOTAL_TIMEOUT_SEC = 15
MAX_RETRIES = 3
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class AirzoneAPI:
    """Client to interact with the Airzone Cloud API."""

    def __init__(self, username: str, password: str, session: aiohttp.ClientSession):
        """Initialize credentials and the aiohttp session provided by HA."""
        self._username = username
        self._password = password
        self._session = session
        self.token: str = ""

    # ----------------------------
    # Internal helpers
    # ----------------------------
    def _auth_params(self) -> Dict[str, str]:
        """Build standard auth params for most endpoints."""
        # Never log these values; keep them internal.
        return {"format": "json", "user_email": self._username, "user_token": self.token}

    @staticmethod
    def _redact(value: Optional[str]) -> str:
        """Redact sensitive values for logging."""
        if not value:
            return ""
        return "***"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
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
                    # Retry on transient/ratelimit errors
                    if resp.status in RETRYABLE_STATUSES and attempt <= max_retries:
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
                    # Some endpoints might return no body; still return None
                    text = await resp.text()
                    return text

            except aiohttp.ClientResponseError as e:
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
            _LOGGER.error("Login failed: no token received (email=%s).", self._redact(self._username))
            return False
        _LOGGER.debug("Login OK (token=%s).", self._redact(self.token))
        return True

    async def fetch_installations(self) -> List[Dict[str, Any]]:
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

    async def fetch_devices(self, installation_id: str) -> List[Dict[str, Any]]:
        """GET /devices for a given installation_id."""
        params = self._auth_params()
        params["installation_id"] = installation_id
        data = await self._request(
            "GET",
            API_DEVICES,
            params=params,
        )
        return (data or {}).get("devices", [])

    async def send_event(self, payload: Dict[str, Any]) -> Any:
        """POST /events with standard headers for event commands (P1..P8 etc.)."""
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
        """PUT /devices/{id} to change scenary: 'sleep' | 'occupied' | 'vacant'.

        Validated via manually tested cURL commands against dkn.airzonecloud.com.
        """
        params = self._auth_params()
        # Endpoint style: /devices/<id>?format=json&user_email=...&user_token=...
        path = f"{API_DEVICES}/{device_id}"
        payload = {"device": {"scenary": scenary}}
        return await self._request("PUT", path, params=params, json=payload)
