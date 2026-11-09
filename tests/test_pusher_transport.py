"""Pusher protocol tests using synthetic frames and an in-memory socket."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType

from custom_components.airzoneclouddaikin.pusher_client import (
    PusherClient,
    PusherProtocolError,
)


def frame(event, data=None, channel=None):
    return SimpleNamespace(
        type=WSMsgType.TEXT,
        data=json.dumps({"event": event, "data": data, "channel": channel}),
    )


def client(messages):
    socket = AsyncMock()
    socket.receive.side_effect = messages
    context = AsyncMock()
    context.__aenter__.return_value = socket
    session = Mock()
    session.ws_connect.return_value = context
    authorize = AsyncMock(return_value="synthetic-signature")
    device = AsyncMock()
    subscription = AsyncMock()
    return (
        PusherClient(
            session,
            "wss://example.invalid/app/synthetic",
            {"private-fake": "fake"},
            authorize,
            device,
            subscription,
        ),
        socket,
        context,
        authorize,
        device,
        subscription,
    )


async def test_authorization_subscription_delivery_and_close():
    c, socket, context, authorize, device, subscription = client(
        [
            frame(
                "pusher:connection_established",
                json.dumps({"socket_id": "1.2", "activity_timeout": 30}),
            ),
            frame("device", {"power": "0"}, "private-fake"),
            frame("pusher_internal:subscription_succeeded", {}, "private-fake"),
            frame("device", {"power": "1"}, "private-unknown"),
            frame("device", {"power": "1"}, "private-fake"),
            frame("pusher:ping", {}),
            SimpleNamespace(type=WSMsgType.CLOSED),
        ]
    )
    await c.connect()
    authorize.assert_awaited_once_with("1.2", "private-fake")
    device.assert_awaited_once_with("fake", {"power": "1"})
    assert subscription.await_args_list[0].args == ("fake", True)
    assert subscription.await_args_list[-1].args == ("fake", False)
    assert socket.send_json.await_args_list[-1].args == (
        {"event": "pusher:pong", "data": {}},
    )
    context.__aexit__.assert_awaited_once()


async def test_cancellation_closes_socket_and_marks_subscription_disconnected():
    c, socket, context, _, _, subscription = client([])
    started = asyncio.Event()
    messages = iter(
        [
            frame(
                "pusher:connection_established",
                {"socket_id": "1.2", "activity_timeout": 30},
            ),
            frame("pusher_internal:subscription_succeeded", {}, "private-fake"),
        ]
    )

    async def receive():
        try:
            return next(messages)
        except StopIteration:
            started.set()
            await asyncio.Event().wait()

    socket.receive.side_effect = receive
    task = asyncio.create_task(c.connect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    context.__aexit__.assert_awaited_once()
    assert subscription.await_args_list[-1].args == ("fake", False)


async def test_remote_error_never_enters_exception_text():
    c, *_ = client([frame("pusher:error", {"message": "synthetic-secret"})])
    with pytest.raises(PusherProtocolError) as error:
        await c.connect()
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("value", [None, [], "broken"])
def test_invalid_object_has_sanitized_error(value):
    with pytest.raises(PusherProtocolError):
        PusherClient._object(value)


def test_invalid_json_exception_has_no_sensitive_decoder_context():
    with pytest.raises(PusherProtocolError) as error:
        PusherClient._object("synthetic-sensitive-invalid-json")
    assert error.value.__context__ is None
