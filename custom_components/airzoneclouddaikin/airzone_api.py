"""Airzone Cloud API client (dkn.airzonecloud.com).

Auth & resilience:
- No "silent re-login": HTTP 401 is surfaced so the coordinator opens a reauth flow.
- Backoff retries transient 429 and read-only 5xx; ambiguous 5xx writes are not replayed.
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
- Read-only calls with retry=True permit ONE gentle timeout retry.
- Automatic retry=False operations delegate backoff to their scheduler.

Important:
- 401 is never retried here; it must bubble up to the coordinator.
- Legacy "scenary" helpers have been **removed**. Use put_device_fields(...) instead.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from json import JSONDecodeError
from typing import Any

from aiohttp import (
    ClientConnectorError,
    ClientOSError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
)
from aiohttp.client_reqrep import RequestInfo
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from multidict import CIMultiDict
from yarl import URL

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
_MAX_WRITE_COOLDOWN_WAIT = 10.0


class CommandNotSentCooldown(HomeAssistantError):
    """A local cooldown rejected this invocation before any HTTP attempt."""

    def __init__(self, retry_at: float):
        super().__init__("Cloud rate limit active; device command was not sent")
        self.retry_at = retry_at


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
        self._suspended = False
        self._auth_suspended = False
        self.request_allowed = lambda: True
        self.on_information_started = None
        self._pending = set()
        self.on_write_auth_failure: Callable[[], None] | None = None

    def suspend(self, *, auth: bool = True):
        """Fence future sends and cancel other pending authenticated operations."""
        self._suspended = True
        self._auth_suspended = self._auth_suspended or auth
        for task in tuple(self._pending):
            if task is not asyncio.current_task():
                task.cancel()

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
        data: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """HTTP request helper (never logs full URLs with secrets)."""
        url = f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        headers = {"User-Agent": USER_AGENT}
        if extra_headers:
            headers.update(extra_headers)

        timeout = ClientTimeout(total=REQUEST_TIMEOUT)
        spath = self._safe_path(path)

        safe_error: Exception
        try:
            if self._suspended or not self.request_allowed():
                raise asyncio.CancelledError
            if (
                self.on_information_started
                and isinstance(json, dict)
                and (event := json.get("event", {})).get("cgi") == "infomaquina"
            ):
                self.on_information_started(str(event.get("device_id")))
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=timeout,
                **({"data": data} if data is not None else {}),
            ) as resp:
                resp.raise_for_status()
                empty_body = resp.status == 204 or resp.content_length == 0
                if resp.content_type == "application/json":
                    try:
                        return await resp.json()
                    except JSONDecodeError, ValueError:
                        body = (await resp.text()).strip()
                        if not body:
                            return None
                        raise
                if empty_body:
                    return ""
                return await resp.text()
        except ClientResponseError as cre:
            if cre.status == 401:
                self.suspend()
            _LOGGER.debug(
                "HTTP %s %s failed with status %s",
                method,
                spath,
                cre.status,
            )
            # aiohttp includes the full request URL (with user_email and
            # user_token) in ClientResponseError.__str__. Never propagate it.
            safe_url = URL(f"{BASE_URL.rstrip('/')}{spath}")
            safe_request = RequestInfo(safe_url, method, CIMultiDict(), safe_url)
            retry_after = cre.headers.get("Retry-After") if cre.headers else None
            headers = {"Retry-After": retry_after} if retry_after else None
            safe_error = ClientResponseError(
                safe_request, history=(), status=cre.status, headers=headers
            )
        except ClientConnectorError:
            _LOGGER.debug("HTTP %s %s connection error", method, spath)
            raise
        except ClientOSError as err:
            # aiohttp can embed the authenticated request URL in this exception
            # when sending a JSON body fails (including the device path).
            # Keep the transport error type/errno without its sensitive text.
            _LOGGER.debug("HTTP %s %s transport error", method, spath)
            error_number = err.errno if isinstance(err.errno, int) else 0
            safe_error = ClientOSError(error_number, "Cloud transport error")
        except TimeoutError:
            _LOGGER.debug("HTTP %s %s timed out", method, spath)
            safe_error = TimeoutError("Cloud request timed out")

        # Raise *outside* the except blocks so Python records no implicit
        # __context__. The original secret-bearing exception is not reachable
        # from the sanitized error. Do not move this raise into a handler.
        raise safe_error

    async def _authed_request_with_retries(self, *args, **kwargs):
        task = asyncio.create_task(self._authed_request_impl(*args, **kwargs))
        self._pending.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            if (
                self._auth_suspended
                and args
                and (
                    args[0] == "PUT"
                    or (
                        args[0] == "POST"
                        and args[1] == API_EVENTS
                        and (kwargs.get("json") or {}).get("event", {}).get("cgi")
                        != "infomaquina"
                    )
                )
            ):
                raise ConfigEntryAuthFailed("Entry authentication suspended") from None
            raise
        finally:
            self._pending.discard(task)

    async def _authed_request_impl(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """Authenticated request with optional 429/5xx and read-only timeout retries.

        English:
        - 401 is *not* retried here; it is propagated so the coordinator opens reauth.
        """
        is_device_write = method.upper() in {"PUT", "PATCH", "DELETE"} or (
            method.upper() == "POST"
            and path == API_EVENTS
            and (json or {}).get("event", {}).get("cgi") != "infomaquina"
        )
        attempt = 0
        while True:
            if self._suspended or not self.request_allowed():
                raise asyncio.CancelledError
            while (remaining := self._cooldown_until - self._now()) > 0:
                if self._suspended or not self.request_allowed():
                    raise asyncio.CancelledError
                if is_device_write and remaining > _MAX_WRITE_COOLDOWN_WAIT:
                    if attempt == 0:
                        raise CommandNotSentCooldown(self._cooldown_until)
                    raise HomeAssistantError(
                        "Cloud rate limit active; device command was not sent"
                    )
                await self._sleep(remaining)

            if self._suspended or not self.request_allowed():
                raise asyncio.CancelledError
            try:
                return await self._request(
                    method,
                    path,
                    params=params,
                    json=json,
                    extra_headers=extra_headers,
                    **({"data": data} if data is not None else {}),
                )
            except ClientResponseError as cre:
                status = cre.status or 0

                # Reads are managed by the coordinator. Commands need to open
                # native HA reauth themselves: a service exception alone does
                # not ask Home Assistant to start a reauth flow.
                if status == 401:
                    self.suspend()
                    if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                        if self.on_write_auth_failure is not None:
                            self.on_write_auth_failure()
                    raise

                # Backoff for 429 and 5xx (transient errors)
                if status == 429 or 500 <= status <= 599:
                    delay = _BASE_DELAY * (2**attempt) + random.uniform(0.0, _JITTER)
                    if status == 429:
                        try:
                            retry_after = cre.headers.get("Retry-After")  # type: ignore[attr-defined]
                            if retry_after:
                                try:
                                    seconds = float(retry_after)
                                except ValueError:
                                    date = parsedate_to_datetime(retry_after)
                                    if date.tzinfo is None:
                                        date = date.replace(tzinfo=UTC)
                                    seconds = (date - datetime.now(UTC)).total_seconds()
                                if math.isfinite(seconds):
                                    delay = max(delay, seconds)
                        except Exception:  # noqa: BLE001
                            pass
                        self._cooldown_until = max(
                            self._cooldown_until, self._now() + delay
                        )
                    # An HTTP 5xx does not prove the server rejected a
                    # control request; P1/P2 or a device PUT might already
                    # have been accepted before the error response. Surface
                    # the unknown outcome to the caller instead of sending
                    # the same physical command twice.
                    if is_device_write and 500 <= status <= 599:
                        raise
                    # 429 explicitly refuses the command: retain the existing
                    # bounded cooldown retry, never enqueue a delayed control.
                    if (
                        status == 429
                        and is_device_write
                        and delay > _MAX_WRITE_COOLDOWN_WAIT
                    ):
                        raise
                    if not retry or attempt >= _MAX_RETRIES:
                        raise
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
                if is_device_write or not retry or attempt >= 1:
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
    async def authorize_channel(self, socket_id: str, channel: str) -> str:
        """Authorize a private Channels subscription using the current token."""
        response = await self._authed_request_with_retries(
            "POST",
            "/pusher/auth",
            data=self._auth_params()
            | {"socket_id": socket_id, "channel_name": channel},
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
            retry=False,
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("auth"), str)
            or not response["auth"]
        ):
            raise ValueError("Invalid channel authorization response")
        return response["auth"]

    async def request_information(self, device_id: str) -> Any:
        """Request fresh information once; the scheduler owns retry timing."""
        return await self._authed_request_with_retries(
            "POST",
            API_EVENTS,
            params=self._auth_params(),
            json={
                "event": {
                    "cgi": "infomaquina",
                    "device_id": device_id,
                    "option": "",
                    "value": "",
                }
            },
            extra_headers=HEADERS_EVENTS,
            retry=False,
        )

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

    async def fetch_installations(
        self, *, retry: bool = True
    ) -> list[dict[str, Any]] | None:
        """GET /installation_relations with backoff for 429/5xx (401 bubbles up)."""
        params = self._auth_params() | {"format": "json"}
        resp = await self._authed_request_with_retries(
            "GET",
            API_INSTALLATION_RELATIONS,
            params=params,
            retry=retry,
        )
        if isinstance(resp, dict) and "installation_relations" in resp:
            relations = resp["installation_relations"]
        else:
            relations = resp
        # An HTTP 200 with a malformed list is not a successful snapshot.
        # Keep the contract focused on the item type, not optional fields.
        if isinstance(relations, list) and all(
            isinstance(relation, dict) for relation in relations
        ):
            return relations
        return None

    async def fetch_devices(
        self, installation_id: Any, *, retry: bool = True
    ) -> list[dict[str, Any]] | None:
        """GET /devices with backoff for 429/5xx (401 bubbles up)."""
        params = self._auth_params() | {
            "format": "json",
            "installation_id": str(installation_id),
        }
        resp = await self._authed_request_with_retries(
            "GET",
            API_DEVICES,
            params=params,
            retry=retry,
        )
        if isinstance(resp, dict) and "devices" in resp:
            devices = resp["devices"]
        else:
            devices = resp
        # A malformed success body is not an authoritative empty snapshot.
        if isinstance(devices, list) and all(
            isinstance(device, dict) for device in devices
        ):
            return devices
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
            if cre.status == 401:
                raise ConfigEntryAuthFailed("Authentication failed") from None
            error_map = {
                422: "wserver_not_connected",
                423: "machine_not_ready",
            }
            if cre.status in error_map:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=error_map[cre.status],
                ) from None
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="event_failed",
                translation_placeholders={"status": str(cre.status)},
            ) from None

    async def put_device_fields(
        self, device_id: str, payload: dict[str, Any], *, retry: bool = True
    ) -> Any:
        """PUT /devices/{id} with provided payload (retries for 429/5xx).

        Canonical method for *all* device field writes (including what used to be "scenary").
        Example:
            await api.put_device_fields("123", {"device": {"preset": "sleep"}})
        """
        params = self._auth_params() | {"format": "json"}
        path = f"{API_DEVICES}/{device_id}"
        try:
            return await self._authed_request_with_retries(
                "PUT", path, params=params, json=payload, retry=retry
            )
        except ClientResponseError as cre:
            if cre.status == 401:
                raise ConfigEntryAuthFailed("Authentication failed") from None
            raise

    async def async_set_scenary(
        self, device_id: str, scenary: str, *, retry: bool = True
    ) -> Any:
        """Set device scenary using the canonical device fields endpoint."""

        return await self.put_device_fields(
            device_id, {"device": {"scenary": scenary}}, retry=retry
        )
