"""Bind verified vendor contracts to the transport and synchronization owner."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import ClientSession

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
                raise PusherProtocolError("Invalid device event") from None
            if not isinstance(patch, dict):
                raise PusherProtocolError("Invalid device event")
            await manager.event(device_id, patch, generation)

        client = PusherClient(
            self._session,
            self._url,
            {f"private-{device}": device for device in manager.schedulers},
            self._authorize,
            device,
            manager.subscription,
        )
        await client.connect()


def build_verified_adapter(api, session: ClientSession) -> DknPushAdapter | None:
    """Keep activation closed until the service-specific contracts are verified.

    The runtime calls this factory again after reauth/reload so an adapter must
    never retain an earlier API instance or authorization token across lifetimes.
    """
    return None
