"""Synthetic, local-only backend and lifecycle check for an independent HA process."""

import asyncio
import json
from pathlib import Path

from aiohttp import ClientSession, web
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED


async def async_setup(hass, config):
    import custom_components.airzoneclouddaikin as integration
    from scripts.lab_policy import LocalReadOnlySession as ReadOnlySession

    calls = []
    power = ["1"]
    outage = [False]
    deny_second = [True]
    sockets = []
    fallback_starts = []

    async def backend(request):
        calls.append((request.method, request.path))
        if outage[0] and request.path in {"/devices", "/events"}:
            fallback_starts.append(asyncio.get_running_loop().time())
        if request.path == "/installation_relations":
            return web.json_response(
                [{"installation_id": "site"}, {"installation_id": "site2"}]
            )
        if request.path == "/devices":
            return web.json_response(
                [
                    {
                        "id": "device"
                        if request.query.get("installation_id") == "site"
                        else "device2",
                        "name": "Synthetic"
                        if request.query.get("installation_id") == "site"
                        else "Synthetic Two",
                        "power": power[0],
                        "mac": "02:00:00:00:00:01"
                        if request.query.get("installation_id") == "site"
                        else "02:00:00:00:00:02",
                        "mode": "1",
                        "modes": "11111",
                        "scenary": "occupied",
                        "local_temp": "22",
                        "cold_consign": "24",
                        "heat_consign": "20",
                        "availables_speeds": "3",
                        "connection_date": "2024-01-01T00:00:00+00:00",
                    }
                ]
            )
        if request.path == "/pusher/auth":
            form = await request.post()
            if form.get("channel_name") == "private-device2" and deny_second[0]:
                return web.Response(status=403)
            return web.json_response({"auth": "synthetic"})
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
            payload = json.loads(frame.data)
            if payload["event"] == "pusher:subscribe":
                channel = payload["data"]["channel"]
                power[0] = "0"
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
                        "data": json.dumps(
                            {"id": channel.removeprefix("private-"), "power": "0"}
                        ),
                    }
                )
        return ws

    app = web.Application()
    app.router.add_get("/socket", socket)
    app.router.add_route("*", "/{path:.*}", backend)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    session = ClientSession(trust_env=False)
    gateway = ReadOnlySession(session, f"http://127.0.0.1:{port}")
    integration.async_get_clientsession = lambda _: gateway

    async def scenario(_):
        report = {}
        entry = ConfigEntry(
            version=2,
            minor_version=1,
            domain="airzoneclouddaikin",
            data={"username": "synthetic@example.invalid"},
            options={"user_token": "synthetic", "scan_interval": 30},
            source="user",
            title="Synthetic",
            unique_id="synthetic",
            discovery_keys={},
            subentries_data=[],
        )
        try:
            await hass.config_entries.async_add(entry)
            async with asyncio.timeout(60):
                while getattr(entry, "runtime_data", None) is None or not hass.data.get(
                    "airzoneclouddaikin", {}
                ).get(entry.entry_id, {}).get("sync_manager"):
                    await asyncio.sleep(0.1)
                manager = hass.data["airzoneclouddaikin"][entry.entry_id][
                    "sync_manager"
                ]
                while (
                    not manager.schedulers["device"].status.push_active
                    or manager.channel_errors.get("device2") != 403
                ):
                    await asyncio.sleep(0.1)
            async with asyncio.timeout(5):
                while entry.runtime_data.coordinator.data["device"]["power"] != "0":
                    await asyncio.sleep(0.05)
            from homeassistant.helpers import entity_registry as er

            climate_id = er.async_get(hass).async_get_entity_id(
                "climate", "airzoneclouddaikin", "device_climate"
            )
            report["healthy_climate_during_403"] = hass.states.get(climate_id).state
            report["channel_403_isolated"] = (
                manager.schedulers["device"].status.push_active
                and not manager.schedulers["device2"].status.push_active
            )
            deny_second[0] = False
            async with asyncio.timeout(100):
                while not manager.schedulers["device2"].status.push_active:
                    await asyncio.sleep(0.1)
            report["channel_recovered"] = (
                manager.schedulers["device"].status.push_active
                and "device2" not in manager.channel_errors
            )
            report["migrated"] = (
                entry.version == 3 and "scan_interval" not in entry.options
            )
            report["entities"] = len(
                [
                    s
                    for s in hass.states.async_all()
                    if "synthetic" in s.entity_id.lower()
                ]
            )
            report["push_power"] = entry.runtime_data.coordinator.data["device"][
                "power"
            ]
            try:
                await entry.runtime_data.api.async_set_scenary("device", "occupied")
            except PermissionError:
                report["control_blocked"] = True
            report["installations"] = len(set(manager._installations.values()))
            initial_gets = sum(path == "/devices" for _, path in calls)
            outage[0] = True
            await sockets[-1].close()
            power[0] = "1"
            async with asyncio.timeout(140):
                while sum(
                    path == "/devices" for _, path in calls
                ) < initial_gets + 2 or any(
                    d.get("power") != "1"
                    for d in entry.runtime_data.coordinator.data.values()
                ):
                    await asyncio.sleep(0.1)
            report["fallback_state_applied"] = all(
                d.get("power") == "1"
                for d in entry.runtime_data.coordinator.data.values()
            )
            report["fallback_gets"] = (
                sum(path == "/devices" for _, path in calls) - initial_gets
            )
            report["information_failures"] = sum(
                s.status.failures for s in manager.schedulers.values()
            )
            report["fallback_spacing"] = all(
                b - a >= 9.9
                for a, b in zip(fallback_starts, fallback_starts[1:], strict=False)
            )
            outage[0] = False
            async with asyncio.timeout(100):
                while not all(
                    s.status.push_active for s in manager.schedulers.values()
                ):
                    await asyncio.sleep(0.1)
            report["push_recovered"] = True
            await hass.config_entries.async_reload(entry.entry_id)
            await asyncio.sleep(1)
            replacement = hass.data["airzoneclouddaikin"][entry.entry_id][
                "sync_manager"
            ]
            await hass.config_entries.async_unload(entry.entry_id)
            report["owned_tasks_closed"] = (
                not manager._tasks
                and not replacement._tasks
                and all(s._task is None for s in replacement.schedulers.values())
            )
            report["backend_control_writes"] = sum(
                method == "PUT" for method, _ in calls
            )
        except Exception as error:
            import traceback

            traceback.print_exc()
            report["error"] = type(error).__name__
        finally:
            bucket = hass.data.get("airzoneclouddaikin", {}).get(entry.entry_id, {})
            if owner := bucket.get("sync_manager"):
                await owner.close()
            await runner.cleanup()
            await session.close()
            report["session_closed"] = session.closed
            report["server_closed"] = not runner.sites
            Path(hass.config.path("lab-result.json")).write_text(json.dumps(report))
            hass.data["lab_finished"].set()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, scenario)
    return True
