"""The real-destination policy is tested without delegating network calls."""

from unittest.mock import Mock

import pytest

from scripts.lab_policy import ProtectedSession, ReadOnlyPolicy


@pytest.mark.parametrize(
    "url",
    [
        "http://dkn.airzonecloud.com/devices",
        "https://dkn.airzonecloud.com:444/devices",
        "https://other.invalid/devices",
        "https://user@dkn.airzonecloud.com/devices",
        "https://dkn.airzonecloud.com/devices?user_token=synthetic",
        "https://dkn.airzonecloud.com/devices#fragment",
    ],
)
def test_wrong_destination_denied(url):
    with pytest.raises(PermissionError):
        ReadOnlyPolicy({"d"}, {"i"}, "synthetic").validate("GET", url)


@pytest.mark.parametrize("cgi", ["modmaquina", "unknown", "infomaquina"])
def test_exact_information_body_and_disabled_transport(cgi):
    policy = ReadOnlyPolicy({"d"}, {"i"}, "synthetic")
    payload = {"event": {"cgi": cgi, "device_id": "d", "option": "", "value": ""}}
    url = "https://dkn.airzonecloud.com/events"
    if cgi == "infomaquina":
        policy.validate("POST", url, json=payload)
    else:
        with pytest.raises(PermissionError):
            policy.validate("POST", url, json=payload)
    session = Mock()
    with pytest.raises(PermissionError):
        ProtectedSession(session, policy).request("POST", url, json=payload)
    session.request.assert_not_called()


async def test_redirect_guard_stops_before_following_location(
    socket_enabled, aiohttp_server
):
    from aiohttp import web

    from scripts.lab_policy import create_protected_session

    reached = []

    async def redirect(_):
        raise web.HTTPFound("/forbidden")

    async def forbidden(_):
        reached.append(True)
        return web.Response()

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/forbidden", forbidden)
    server = await aiohttp_server(app)
    protected = await create_protected_session(
        ReadOnlyPolicy({"d"}, {"i"}, "synthetic")
    )
    try:
        with pytest.raises(PermissionError):
            async with protected.session.get(server.make_url("/redirect")):
                pass
        assert not reached
    finally:
        await protected.session.close()


async def test_socket_cannot_send_unknown_commands():
    from unittest.mock import AsyncMock

    from scripts.lab_policy import ProtectedSocket

    raw = AsyncMock()
    socket = ProtectedSocket(raw, ReadOnlyPolicy({"d"}, {"i"}, "synthetic"))
    for payload in [
        {"event": "device", "data": {"cgi": "modmaquina"}},
        {"event": "client-command", "data": {}},
        {
            "event": "pusher:subscribe",
            "data": {"channel": "private-other", "auth": "synthetic"},
        },
    ]:
        with pytest.raises(PermissionError):
            await socket.send_json(payload)
    raw.send_json.assert_not_awaited()
    await socket.send_json({"event": "pusher:ping", "data": {}})
    raw.send_json.assert_awaited_once()
