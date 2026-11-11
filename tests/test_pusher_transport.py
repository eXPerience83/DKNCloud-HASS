"""Pusher protocol tests using synthetic frames and an in-memory socket."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ServerDisconnectedError, WSMsgType

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


async def test_authorization_wait_does_not_block_ping_and_is_joined_on_cancel():
    c, socket, _, authorize, _, _ = client([])
    authorized, pong, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    messages = iter(
        [
            frame(
                "pusher:connection_established",
                {"socket_id": "1.2", "activity_timeout": 30},
            ),
            frame("pusher:ping", {}),
        ]
    )

    async def auth(*_):
        authorized.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def receive():
        try:
            return next(messages)
        except StopIteration:
            pong.set()
            await asyncio.Event().wait()

    authorize.side_effect = auth
    socket.receive.side_effect = receive
    task = asyncio.create_task(c.connect())
    await authorized.wait()
    await pong.wait()
    assert {"event": "pusher:pong", "data": {}} in [
        call.args[0] for call in socket.send_json.await_args_list
    ]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_pending_authorization_is_joined_before_socket_transport_closes():
    c, socket, context, authorize, _, _ = client([])
    entered, cleaned = asyncio.Event(), asyncio.Event()
    handshake = frame(
        "pusher:connection_established", {"socket_id": "1.2", "activity_timeout": 30}
    )

    async def auth(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def receive():
        if not entered.is_set():
            return handshake
        await asyncio.Event().wait()

    async def closing(*_):
        assert cleaned.is_set(), "Authorization I/O must end before transport close"

    authorize.side_effect = auth
    socket.receive.side_effect = receive
    context.__aexit__.side_effect = closing
    task = asyncio.create_task(c.connect())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "failure",
    [403, 429, 500, TimeoutError, ValueError, OSError, ServerDisconnectedError, None],
)
async def test_denied_channel_recovers_without_interrupting_healthy_channel(failure):
    from aiohttp import ClientResponseError

    c, socket, _, authorize, device, subscription = client([])
    c._channels = {"private-bad": "bad", "private-good": "good"}
    retry, healthy, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    queue = asyncio.Queue()
    await queue.put(
        frame(
            "pusher:connection_established",
            {"socket_id": "1.2", "activity_timeout": 30},
        )
    )
    attempts = []

    async def auth(_, channel):
        attempts.append(channel)
        if channel == "private-bad" and attempts.count(channel) == 1:
            if failure is None:
                return None
            if isinstance(failure, int):
                raise ClientResponseError(None, (), status=failure)
            raise failure("synthetic")
        return "synthetic"

    async def wait(_):
        await retry.wait()

    async def send(payload):
        if payload["event"] == "pusher:subscribe":
            channel = payload["data"]["channel"]
            await queue.put(
                frame("pusher_internal:subscription_succeeded", {}, channel)
            )
            await queue.put(frame("device", {"power": "1"}, channel))

    async def received(device_id, payload):
        (healthy if device_id == "good" else recovered).set()

    authorize.side_effect = auth
    c._retry_sleep = wait
    c._on_device = received
    socket.send_json.side_effect = send
    socket.receive.side_effect = queue.get
    task = asyncio.create_task(c.connect())
    try:
        await asyncio.wait_for(healthy.wait(), 2)
        assert not recovered.is_set()
        retry.set()
        await asyncio.wait_for(recovered.wait(), 2)
        assert attempts.count("private-good") == 1
        assert ("good", False) not in [
            call.args for call in subscription.await_args_list
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_channel_401_still_reaches_account_supervisor():
    from aiohttp import ClientResponseError

    c, _, _, authorize, _, _ = client(
        [
            frame(
                "pusher:connection_established",
                {"socket_id": "1.2", "activity_timeout": 30},
            ),
        ]
    )
    authorize.side_effect = ClientResponseError(None, (), status=401)
    with pytest.raises(ClientResponseError) as error:
        await c.connect()
    assert error.value.status == 401
    authorize.assert_awaited_once()


async def test_missing_ack_retries_only_affected_channel():
    c, socket, _, authorize, _, subscription = client([])
    c._channels = {"private-noack": "noack", "private-healthy": "healthy"}
    c._ack_timeout = 0.02
    queue = asyncio.Queue()
    await queue.put(
        frame(
            "pusher:connection_established",
            {"socket_id": "1.2", "activity_timeout": 30},
        )
    )
    retry_sent = asyncio.Event()
    attempts = []

    async def send(payload):
        if payload["event"] != "pusher:subscribe":
            return
        channel = payload["data"]["channel"]
        attempts.append(channel)
        if channel == "private-healthy":
            await queue.put(
                frame("pusher_internal:subscription_succeeded", {}, channel)
            )
        if attempts.count("private-noack") >= 2:
            retry_sent.set()

    c._retry_sleep = lambda _: asyncio.sleep(0)
    socket.send_json.side_effect = send
    socket.receive.side_effect = queue.get
    task = asyncio.create_task(c.connect())
    try:
        await asyncio.wait_for(retry_sent.wait(), 2)
        assert authorize.await_count >= 3
        assert attempts.count("private-healthy") == 1
        assert attempts.count("private-noack") >= 2
        assert ("healthy", True) in [call.args for call in subscription.await_args_list]
        assert ("healthy", False) not in [
            call.args for call in subscription.await_args_list
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_late_ack_during_reauthorization_does_not_resubscribe():
    c, socket, _, authorize, _, subscription = client([])
    c._ack_timeout = 0.01
    c._retry_sleep = lambda _: asyncio.sleep(0)
    queue = asyncio.Queue()
    queue.put_nowait(
        frame(
            "pusher:connection_established",
            {"socket_id": "1.2", "activity_timeout": 30},
        )
    )
    pending, release, applied = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = 0

    async def authorization(*_):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            pending.set()
            await release.wait()
        return "synthetic-signature"

    async def subscribed(device, active):
        if active:
            applied.set()

    authorize.side_effect = authorization
    subscription.side_effect = subscribed
    socket.receive.side_effect = queue.get
    task = asyncio.create_task(c.connect())
    try:
        await asyncio.wait_for(pending.wait(), 2)
        queue.put_nowait(
            frame("pusher_internal:subscription_succeeded", {}, "private-fake")
        )
        await asyncio.wait_for(applied.wait(), 2)
        subscription.reset_mock()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert socket.send_json.await_count == 1
        assert ("fake", False) not in [
            call.args for call in subscription.await_args_list
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
