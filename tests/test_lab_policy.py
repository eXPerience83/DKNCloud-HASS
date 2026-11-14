"""The real-destination policy is tested without delegating network calls."""

from unittest.mock import Mock

import pytest

from scripts.lab_policy import ProtectedSession, ReadOnlyPolicy


def test_only_service_socket_origin_is_permitted():
    policy = ReadOnlyPolicy({"d"}, {"i"}, "synthetic")
    path = "/app/synthetic?protocol=7&client=python-dkncloud&version=1.0"
    policy.validate_socket("wss://dkn.airzonecloud.com:8080" + path)
    for origin in (
        "wss://ws.pusherapp.com",
        "wss://dkn.airzonecloud.com",
        "wss://dkn.airzonecloud.com:8081",
        "ws://dkn.airzonecloud.com:8080",
        "wss://other.invalid:8080",
    ):
        with pytest.raises(PermissionError):
            policy.validate_socket(origin + path)


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


def test_method_override_header_is_denied_before_delegation():
    policy = ReadOnlyPolicy({"d"}, {"i"}, "synthetic", enabled=True)
    raw = Mock()
    with pytest.raises(PermissionError):
        ProtectedSession(raw, policy).request(
            "GET",
            "https://dkn.airzonecloud.com/devices",
            headers={"X-HTTP-Method-Override": "PUT"},
        )
    raw.request.assert_not_called()


def test_real_policy_quota_and_deadline(monkeypatch):
    from scripts import lab_policy

    policy = ReadOnlyPolicy({"d"}, {"i"}, "synthetic", enabled=True)
    session = Mock()
    protected = ProtectedSession(session, policy)
    for _ in range(40):
        protected.request("GET", "https://dkn.airzonecloud.com/devices")
    with pytest.raises(PermissionError, match="quota"):
        protected.request("GET", "https://dkn.airzonecloud.com/devices")
    assert session.request.call_count == 40
    from types import SimpleNamespace

    monkeypatch.setattr(
        lab_policy, "time", SimpleNamespace(monotonic=lambda: policy.deadline)
    )
    with pytest.raises(PermissionError, match="duration"):
        protected.request("GET", "https://dkn.airzonecloud.com/devices")


def test_loopback_lab_rejects_unverified_compatibility_without_network():
    from scripts.lab_policy import LocalReadOnlySession

    raw = Mock()
    local = LocalReadOnlySession(raw, "http://127.0.0.1:4567/")
    with pytest.raises(ValueError, match="policy rejected"):
        local.compatibility_websocket(
            "wss://dkn.airzonecloud.com:8080/app/synthetic",
            ssl_context=Mock(),
        )
    raw.ws_connect.assert_not_called()


def test_loopback_lab_maps_verified_compatibility_only_to_local_socket():
    from custom_components.airzoneclouddaikin.push_tls import (
        create_compatibility_context,
    )
    from scripts.lab_policy import LocalReadOnlySession

    raw = Mock()
    local = LocalReadOnlySession(raw, "http://127.0.0.1:4567/")
    local.compatibility_websocket(
        "wss://dkn.airzonecloud.com:8080/app/synthetic",
        ssl_context=create_compatibility_context(),
    )
    raw.ws_connect.assert_called_once_with("http://127.0.0.1:4567/socket")


@pytest.mark.parametrize("missing_field", ["device_id", "option", "value"])
def test_loopback_rejects_incomplete_information_payload(missing_field):
    """Offline lab must reject INFO forms forbidden by the real read-only policy."""
    from scripts.lab_policy import LocalReadOnlySession

    raw = Mock()
    local = LocalReadOnlySession(raw, "http://127.0.0.1:4567/")
    event = {"cgi": "infomaquina", "device_id": "d", "option": "", "value": ""}
    event.pop(missing_field)

    with pytest.raises(PermissionError, match="Control operation denied"):
        local.request(
            "POST",
            "https://dkn.airzonecloud.com/events",
            json={"event": event},
        )

    raw.request.assert_not_called()
    assert local.blocked == 1


def test_loopback_preserves_canonical_information_request():
    """A complete INFO event still delegates only to the local test service."""
    from scripts.lab_policy import LocalReadOnlySession

    raw = Mock()
    local = LocalReadOnlySession(raw, "http://127.0.0.1:4567/")
    payload = {
        "event": {
            "cgi": "infomaquina",
            "device_id": "d",
            "option": "",
            "value": "",
        }
    }

    local.request(
        "POST",
        "https://dkn.airzonecloud.com/events",
        json=payload,
    )
    raw.request.assert_called_once_with(
        "POST",
        "http://127.0.0.1:4567/events",
        allow_redirects=False,
        json=payload,
    )
    assert local.blocked == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://dkn.airzonecloud.com/devices",
        "https://other.invalid/devices",
        "https://dkn.airzonecloud.com:444/devices",
        "https://user@dkn.airzonecloud.com/devices",
        "https://dkn.airzonecloud.com/devices?user_token=synthetic",
        "https://dkn.airzonecloud.com/devices#fragment",
    ],
)
def test_loopback_rejects_noncanonical_http_destinations(url):
    """Invalid production destinations must not become valid in the offline LAB."""
    from scripts.lab_policy import LocalReadOnlySession

    raw = Mock()
    local = LocalReadOnlySession(raw, "http://127.0.0.1:4567/")
    with pytest.raises(PermissionError, match="Control operation denied"):
        local.request("GET", url)
    raw.request.assert_not_called()
    assert local.blocked == 1


def test_loopback_forwards_canonical_get_with_separate_auth_params():
    """Keep the canonical production URL and separately supplied auth params."""
    from scripts.lab_policy import LocalReadOnlySession

    raw = Mock()
    local = LocalReadOnlySession(raw, "http://127.0.0.1:4567/")
    params = {"user_email": "synthetic@example.invalid", "user_token": "fake"}

    local.request(
        "GET",
        "https://dkn.airzonecloud.com/devices",
        params=params,
    )
    raw.request.assert_called_once_with(
        "GET",
        "http://127.0.0.1:4567/devices",
        allow_redirects=False,
        params=params,
    )
    assert local.blocked == 0
