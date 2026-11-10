"""Bind verified vendor contracts to the transport and synchronization owner."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession
from yarl import URL

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
    ) -> None:
        self._session, self._url = session, url
        self._authorize, self._decode = authorize, decode

    async def connect(self, manager: SyncManager) -> None:
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
        )
        await client.connect()


# Public service application identifier; not an account token or app secret.
PUSHER_APP_KEY = "765ec374ae0a69f4ce44"
PUSHER_HOST = "ws.pusherapp.com"


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
        scheme="wss", host=PUSHER_HOST, path=f"/app/{PUSHER_APP_KEY}"
    ).with_query(protocol="7", client="python-dkncloud", version="1.0")
    return DknPushAdapter(session, str(url), api.authorize_channel, decode_device)
