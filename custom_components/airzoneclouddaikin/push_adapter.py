"""Bind verified vendor contracts to the transport and synchronization owner."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession
from yarl import URL

from . import push_tls
from .push_tls import PUSHER_HOST, PUSHER_PORT
from .pusher_client import PusherClient, PusherProtocolError
from .sync_manager import SyncManager


class DknPushAdapter:
    """Build only from verified connection, authorization and decoding contracts.

    Contract functions are explicit to keep unverified formats out of runtime.
    """

    def __init__(
        self,
        session: ClientSession,
        url: str,
        authorize: Callable[[str, str], Awaitable[str]],
        decode: Callable[[Any], dict[str, Any]],
        *,
        tls_compatibility: bool = False,
    ) -> None:
        self._session, self._url = session, url
        self._authorize, self._decode = authorize, decode
        self._tls_compatibility = tls_compatibility
        self._ssl_context = None
        self._context_lock = asyncio.Lock()

    async def connect(self, manager: SyncManager) -> None:
        if manager._stopped or manager._auth_failed:
            raise asyncio.CancelledError
        options = {}
        # A genuine shared aiohttp session has no per-call redirect hook for
        # ws_connect. Use an isolated guarded socket, never mutate shared REST.
        # Synthetic/laboratory gateway objects retain their own guarded socket.
        if not self._tls_compatibility and isinstance(self._session, ClientSession):
            options["websocket_connect"] = push_tls.strict_websocket
        if self._tls_compatibility:
            async with self._context_lock:
                if self._ssl_context is None:
                    self._ssl_context = await asyncio.to_thread(
                        push_tls.create_compatibility_context
                    )
            push_tls.validate_context(self._url, self._ssl_context)
            # The protected laboratory supplies its own redirect-guarded boundary.
            connector = getattr(self._session, "compatibility_websocket", None)
            options["websocket_connect"] = partial(
                connector or push_tls.compatibility_websocket,
                ssl_context=self._ssl_context,
            )
        if manager._stopped or manager._auth_failed:
            raise asyncio.CancelledError
        generation = manager.state.generation

        async def device(device_id: str, payload: Any) -> None:
            try:
                patch = self._decode(payload)
            except ValueError, TypeError, KeyError:
                patch = None
            if not isinstance(patch, dict):
                raise PusherProtocolError("Invalid device event")
            await manager.event(device_id, patch, generation)

        async def authorize(socket_id, channel):
            async with manager._limit:
                if manager._stopped or manager._auth_failed:
                    raise asyncio.CancelledError
                device_id = channel.removeprefix("private-")
                try:
                    signature = await self._authorize(socket_id, channel)
                except ClientResponseError as error:
                    if error.status != 401:
                        manager.channel_errors[device_id] = error.status
                    raise
                except (ClientError, TimeoutError, OSError, ValueError) as error:
                    manager.channel_errors[device_id] = (
                        "invalid_response"
                        if isinstance(error, ValueError)
                        else "transport"
                    )
                    raise
                manager.channel_errors.pop(device_id, None)
                return signature

        client = PusherClient(
            self._session,
            self._url,
            {f"private-{device}": device for device in manager.schedulers},
            authorize,
            device,
            manager.subscription,
            **options,
        )
        await client.connect()


# Public service application identifier; not an account token or app secret.
PUSHER_APP_KEY = "765ec374ae0a69f4ce44"


def decode_device(payload: Any) -> dict[str, Any]:
    """Decode the public Channels data encoding into a root device patch."""
    value = None
    try:
        value = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError, TypeError:
        pass
    if not isinstance(value, dict) or not value:
        raise PusherProtocolError("Invalid device event")
    if not any(
        k in value
        for k in (
            "id",
            "power",
            "mode",
            "local_temp",
            "connection_date",
            "update_date",
            "scenary",
            "cold_consign",
            "heat_consign",
            "cold_speed",
            "heat_speed",
            "sleep_time",
            "machine_errors",
            "modes",
            "availables_speeds",
            "firmware",
            "brand",
            "units",
            "name",
            "progs_enabled",
            "min_limit_cold",
            "max_limit_cold",
            "min_limit_heat",
            "max_limit_heat",
            "min_temp_unoccupied",
            "max_temp_unoccupied",
            "mac",
            "pin",
            "installation_id",
            "ver_state_slats",
            "ver_position_slats",
            "hor_state_slats",
            "hor_position_slats",
            "ver_cold_slats",
            "ver_heat_slats",
            "hor_cold_slats",
            "hor_heat_slats",
        )
    ):
        raise PusherProtocolError("Unrecognized device event")
    return value


def build_verified_adapter(api, session: ClientSession) -> DknPushAdapter:
    """Build a fresh adapter with the current entry's token-only API."""
    url = URL.build(
        scheme="wss",
        host=PUSHER_HOST,
        port=PUSHER_PORT,
        path=f"/app/{PUSHER_APP_KEY}",
    ).with_query(protocol="7", client="python-dkncloud", version="1.0")
    return DknPushAdapter(
        session,
        str(url),
        api.authorize_channel,
        decode_device,
        tls_compatibility=push_tls.STATIC_RSA_COMPATIBILITY_ENABLED,
    )
