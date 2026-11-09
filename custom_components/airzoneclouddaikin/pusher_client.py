"""Minimal asynchronous Pusher transport using the public Channels protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiohttp import ClientResponseError, ClientSession, WSMsgType
from yarl import URL


class PusherProtocolError(Exception):
    """A sanitized protocol failure; never carries remote payloads or URLs."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class PusherClient:
    """One connection and reader; account authorization remains with the caller.

    The verified connection URL and device event decoder must be supplied by
    the integration. This module neither discovers nor guesses vendor settings.
    """

    def __init__(
        self,
        session: ClientSession,
        url: str,
        channels: Mapping[str, str],
        authorize: Callable[[str, str], Awaitable[str]],
        on_device: Callable[[str, Any], Awaitable[None]],
        on_subscription: Callable[[str, bool], Awaitable[None]],
    ) -> None:
        parsed = URL(url)
        if parsed.scheme != "wss" or parsed.user is not None or parsed.fragment:
            raise ValueError("A secure verified WebSocket endpoint is required")
        self._session = session
        self._url = url
        self._channels = dict(channels)
        self._authorize = authorize
        self._on_device = on_device
        self._on_subscription = on_subscription

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        try:
            result = json.loads(value) if isinstance(value, str) else value
        except ValueError, TypeError:
            result = None
        if not isinstance(result, dict):
            raise PusherProtocolError("Invalid protocol object")
        return result

    async def connect(self) -> None:
        """Consume until disconnection; cancellation closes only this socket."""
        subscribed: set[str] = set()
        authorizations = []
        try:
            async with self._session.ws_connect(
                self._url, autoping=True, max_msg_size=1024 * 1024
            ) as socket:
                activity = 30.0
                awaiting_pong = False
                established = False
                while True:
                    for task in authorizations:
                        if task.done():
                            task.result()
                    try:
                        message = await asyncio.wait_for(
                            socket.receive(),
                            timeout=10.0 if awaiting_pong else activity,
                        )
                    except TimeoutError:
                        if awaiting_pong or not established:
                            raise PusherProtocolError(
                                "Connection activity timed out"
                            ) from None
                        await socket.send_json({"event": "pusher:ping", "data": {}})
                        awaiting_pong = True
                        continue
                    if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
                        code = socket.close_code
                        if isinstance(code, int) and 4000 <= code <= 4299:
                            raise PusherProtocolError(
                                "Channel service closed the connection", code
                            )
                        return
                    if message.type == WSMsgType.ERROR:
                        raise PusherProtocolError("WebSocket transport failed")
                    if message.type != WSMsgType.TEXT:
                        continue
                    frame = self._object(message.data)
                    awaiting_pong = False
                    event = frame.get("event")
                    if event == "pusher:connection_established":
                        if established:
                            raise PusherProtocolError("Repeated connection handshake")
                        data = self._object(frame.get("data"))
                        socket_id = data.get("socket_id")
                        timeout = data.get("activity_timeout")
                        if not isinstance(socket_id, str) or not socket_id:
                            raise PusherProtocolError("Missing socket identity")
                        if (
                            not isinstance(timeout, (int, float))
                            or not 0 < timeout <= 86400
                        ):
                            raise PusherProtocolError("Invalid activity timeout")
                        activity = float(timeout)
                        established = True

                        async def authorize_channels(socket_id=socket_id):
                            for channel in self._channels:
                                try:
                                    signature = await self._authorize(
                                        socket_id, channel
                                    )
                                except ClientResponseError as error:
                                    if error.status != 403:
                                        raise
                                    await self._on_subscription(
                                        self._channels[channel], False
                                    )
                                    raise PusherProtocolError(
                                        "Channel authorization denied", 403
                                    ) from None
                                if not isinstance(signature, str) or not signature:
                                    raise PusherProtocolError(
                                        "Invalid channel authorization"
                                    )
                                await socket.send_json(
                                    {
                                        "event": "pusher:subscribe",
                                        "data": {"channel": channel, "auth": signature},
                                    }
                                )

                        authorizations.append(asyncio.create_task(authorize_channels()))
                        await asyncio.sleep(0)
                    elif event == "pusher:ping":
                        await socket.send_json({"event": "pusher:pong", "data": {}})
                    elif event == "pusher:error":
                        data = self._object(frame.get("data"))
                        code = data.get("code")
                        raise PusherProtocolError(
                            "Channel service rejected the request",
                            code if isinstance(code, int) else None,
                        )
                    else:
                        channel = frame.get("channel")
                        if (
                            not isinstance(channel, str)
                            or channel not in self._channels
                        ):
                            continue
                        device_id = self._channels[channel]
                        if event == "pusher_internal:subscription_succeeded":
                            subscribed.add(channel)
                            await self._on_subscription(device_id, True)
                        elif event == "device" and channel in subscribed:
                            # Vendor decoding is deliberately delegated; raw data is
                            # transient and is never retained in diagnostics.
                            await self._on_device(device_id, frame.get("data"))
        finally:
            for task in authorizations:
                task.cancel()
            await asyncio.gather(*authorizations, return_exceptions=True)
            for channel in subscribed:
                await self._on_subscription(self._channels[channel], False)
