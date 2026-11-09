"""Real HA entry and HTTP/WS clients behind an offline, deny-default gateway."""

import asyncio
import json
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession, web

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import push_adapter


class ReadOnlySession:
    """Test-only boundary; forward permitted operations exclusively to loopback."""

    def __init__(self, session, origin):
        self.session, self.origin = session, origin
        self.blocked = 0

    def request(self, method, url, **kwargs):
        path = urlsplit(str(url)).path.rstrip("/")
        allowed = method == "GET" and path in {"/devices", "/installation_relations"}
        allowed |= method == "POST" and path in {"/users/sign_in", "/pusher/auth"}
        payload = kwargs.get("json") or {}
        event = payload.get("event", {})
        allowed |= (
            method == "POST"
            and path == "/events"
            and set(payload) == {"event"}
            and event.get("cgi") == "infomaquina"
            and set(event) <= {"cgi", "device_id", "option", "value"}
            and event.get("option", "") == ""
            and event.get("value", "") == ""
        )
        if not allowed:
            self.blocked += 1
            raise PermissionError("Control operation denied")
        return self.session.request(
            method, self.origin + path, allow_redirects=False, **kwargs
        )

    def ws_connect(self, url, **kwargs):
        if not str(url).startswith("wss://ws.pusherapp.com/app/"):
            raise PermissionError("Unknown WebSocket denied")
        return self.session.ws_connect(self.origin + "/socket", **kwargs)


async def test_real_entry_loopback_and_control_barrier(
    hass,
    monkeypatch,
    socket_enabled,
    aiohttp_server,
    dkn_config_entry_factory,
    sample_device,
):
    delivered = asyncio.Event()
    calls = []
    outage = [False]
    sockets = []

    async def handler(request):
        calls.append((request.method, request.path))
        if request.path == "/installation_relations":
            return web.json_response([{"installation_id": "synthetic-installation"}])
        if request.path == "/devices":
            return web.json_response([sample_device])
        if request.path == "/pusher/auth":
            return web.json_response({"auth": "synthetic-signature"})
        return web.json_response({}, status=503 if outage[0] else 201)

    async def socket(request):
        if outage[0]:
            return web.Response(status=503)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        await ws.send_json(
            {
                "event": "pusher:connection_established",
                "data": json.dumps({"socket_id": "1.2", "activity_timeout": 30}),
            }
        )
        async for frame in ws:
            message = json.loads(frame.data)
            if message["event"] == "pusher:subscribe":
                channel = message["data"]["channel"]
                await ws.send_json(
                    {
                        "event": "pusher_internal:subscription_succeeded",
                        "channel": channel,
                        "data": "{}",
                    }
                )
                await ws.send_json(
                    {
                        "event": "device",
                        "channel": channel,
                        "data": json.dumps({"id": "dev1", "power": "0"}),
                    }
                )
                delivered.set()
        return ws

    app = web.Application()
    app.router.add_get("/socket", socket)
    app.router.add_route("*", "/{path:.*}", handler)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        gateway = ReadOnlySession(session, str(server.make_url("")).rstrip("/"))
        monkeypatch.setattr(integration, "async_get_clientsession", lambda _: gateway)
        monkeypatch.setattr(
            integration, "build_verified_adapter", push_adapter.build_verified_adapter
        )
        entry = dkn_config_entry_factory()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(delivered.wait(), 5)
        manager = hass.data[integration.DOMAIN][entry.entry_id]["sync_manager"]
        for _ in range(100):
            if entry.runtime_data.coordinator.data["dev1"]["power"] == "0":
                break
            await asyncio.sleep(0.01)
        assert entry.runtime_data.coordinator.data["dev1"]["power"] == "0"

        async def until(predicate, timeout=90):
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(0.05)

        await until(lambda: manager.schedulers["dev1"].status.push_active)
        reads_before = sum(path == "/devices" for _, path in calls)
        outage[0] = True
        await sockets[-1].close()
        await until(lambda: not manager.schedulers["dev1"].status.push_active)
        await until(lambda: sum(path == "/devices" for _, path in calls) > reads_before)
        assert manager.schedulers["dev1"].status.failures > 0
        outage[0] = False
        await until(lambda: manager.schedulers["dev1"].status.push_active)
        with pytest.raises(PermissionError):
            await entry.runtime_data.api.async_set_scenary("dev1", "occupied")
        for method, path, payload in [
            ("POST", "/events", {"event": {"cgi": "modmaquina"}}),
            ("PUT", "/devices/dev1", {}),
            ("POST", "/unknown", {}),
        ]:
            with pytest.raises(PermissionError):
                gateway.request(method, "https://example.invalid" + path, json=payload)
        assert gateway.blocked == 4
        coordinator = entry.runtime_data.coordinator
        coordinator.data["dev1"].update(scenary="sleep", sleep_expired=True)
        coordinator.async_update_listeners()
        await until(lambda: gateway.blocked > 4)
        assert not any(method == "PUT" for method, _ in calls)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not manager._tasks
        assert all(s._task is None for s in manager.schedulers.values())
        assert not any(method == "PUT" for method, _ in calls)
