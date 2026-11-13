"""Real HA entry and HTTP/WS clients behind an offline, deny-default gateway."""

import asyncio
import json

import pytest
from aiohttp import ClientSession, web

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import push_adapter
from scripts.lab_policy import LocalReadOnlySession as ReadOnlySession


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
    control_writes = []
    outage = [False]
    sockets = []

    async def handler(request):
        if request.method == "POST" and request.path not in {
            "/users/sign_in",
            "/pusher/auth",
        }:
            body = await request.json()
            if (
                request.path != "/events"
                or body.get("event", {}).get("cgi") != "infomaquina"
            ):
                control_writes.append("unexpected_post")
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
            if message["event"] == "pusher:ping":
                await ws.send_json({"event": "pusher:pong", "data": {}})
                continue
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
        # The security barrier must also protect an automatic cleanup
        # requested for a session-consistent OFF, not just a timeout hint.
        tracking = hass.data[integration.DOMAIN][entry.entry_id][
            "sleep_tracking"
        ]["dev1"]
        tracking.last_scenary = "sleep"
        tracking.sleep_started_at_utc = integration.dt_util.utcnow()
        tracking.observed_on = True
        tracking.consecutive_off_reports = 2
        coordinator.data["dev1"].update(
            scenary="sleep", power="0", sleep_off_confirmed=True
        )
        coordinator.async_update_listeners()
        await until(lambda: gateway.blocked > 4)
        assert not any(method in {"PUT", "PATCH", "DELETE"} for method, _ in calls)
        assert not control_writes
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not manager._tasks
        assert all(s._task is None for s in manager.schedulers.values())
        assert not any(method in {"PUT", "PATCH", "DELETE"} for method, _ in calls)
        assert not control_writes
