"""One supervised run of the real integration through the TLS-only laboratory."""

import asyncio
import logging
import os
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from scripts.lab_lifecycle import finish_lab
from scripts.lab_policy import ReadOnlyPolicy
from scripts.lab_tls_backend import SyntheticBackend
from scripts.protected_lab import LabSupervisor, create_gateway, real_policy


async def async_setup(hass, config):
    import custom_components.airzoneclouddaikin as integration
    from custom_components.airzoneclouddaikin import config_flow
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
    from custom_components.airzoneclouddaikin.push_adapter import PUSHER_APP_KEY

    # No arbitrary integration log record may escape a future authenticated run.
    root = logging.getLogger()
    root.handlers = [logging.NullHandler()]
    real = os.environ.get("DKN_LAB_TARGET") == "real"
    case = os.environ.get("DKN_LAB_CASE", "normal")
    if real:
        # Gate before reading any account values or constructing a connector.
        from scripts.protected_lab import REAL_DESTINATIONS_ENABLED

        if not REAL_DESTINATIONS_ENABLED:
            raise PermissionError("Real destinations disabled")
        device = os.environ.pop("DKN_DEVICE_ID")
        installation = os.environ.pop("DKN_INSTALLATION_ID")
        username = os.environ.pop("DKN_EMAIL")
        password = os.environ.pop("DKN_PASSWORD")
        policy = real_policy({device}, {installation}, PUSHER_APP_KEY)
        backend = None
        # Reserve time for HA shutdown inside the parent's five-minute deadline.
        supervisor = LabSupervisor(duration=285)
        gateway = await create_gateway(policy, supervisor)
    else:
        device, installation = "device", "site"
        username, password = "synthetic@example.invalid", "synthetic-password"
        supervisor = LabSupervisor(
            duration=120
            if case == "reconnect"
            else 60
            if case in {"normal", "sleep"}
            else 15
            if case in {"cooldown", "authorization", "get"}
            else 3,
            state_limit=1 if case == "quota" else 40,
            spacing=0.01,
        )
        backend = await SyntheticBackend(hass.config.config_dir).start()
        if case in {"duration", "authorization", "get"}:
            backend.wait = asyncio.Event()
            backend.wait_path = {
                "authorization": "/pusher/auth",
                "get": "/devices",
                "duration": "/users/sign_in",
            }[case]
        backend.redirect = case == "redirect"
        policy = ReadOnlyPolicy({device}, {installation}, PUSHER_APP_KEY, enabled=True)
        gateway = await create_gateway(
            policy, supervisor, connector=backend.connector(trusted=case != "tls")
        )
    # Nothing containing account/device data is persisted, even in ephemeral HA storage.
    from homeassistant.helpers.storage import Store

    original_save, original_delay = Store.async_save, Store.async_delay_save

    async def memory_save(*_, **__):
        pass

    Store.async_save = memory_save
    Store.async_delay_save = lambda *_, **__: None
    original = integration.async_get_clientsession
    original_flow = config_flow.async_get_clientsession
    integration.async_get_clientsession = config_flow.async_get_clientsession = (
        lambda _: gateway
    )

    guard = gateway.enforce()
    guard.__enter__()

    def restore_guard():
        guard.__exit__(None, None, None)
        Store.async_save, Store.async_delay_save = original_save, original_delay
        integration.async_get_clientsession = original
        config_flow.async_get_clientsession = original_flow

    hass.data["lab_restore_guard"] = restore_guard

    async def scenario(_):
        report = {}
        entry = None
        watcher = asyncio.create_task(supervisor.watch())

        async def exercise():
            nonlocal entry
            login = AirzoneAPI(username, gateway, password=password)
            if not await login.login():
                raise RuntimeError("Synthetic login failed")
            entry = ConfigEntry(
                version=3,
                minor_version=1,
                domain="airzoneclouddaikin",
                data={"username": username},
                options={"user_token": login._token},
                source="user",
                title="Synthetic",
                unique_id="synthetic",
                discovery_keys={},
                subentries_data=[],
            )
            await hass.config_entries.async_add(entry)
            while not getattr(entry, "runtime_data", None):
                await asyncio.sleep(0.05)
            report["loaded"] = True
            if case == "cooldown":
                entry.runtime_data.api._cooldown_until = (
                    entry.runtime_data.api._now() + 60
                )
                await supervisor.stopped.wait()
                return
            coordinator = entry.runtime_data.coordinator
            manager = hass.data[entry.domain][entry.entry_id]["sync_manager"]
            while not manager.schedulers[device].status.push_active or (
                not real and coordinator.data[device]["power"] != "0"
            ):
                await asyncio.sleep(0.05)
            while not all(
                any(s.domain == domain for s in hass.states.async_all())
                for domain in {"climate", "switch", "number", "sensor", "binary_sensor"}
            ):
                await asyncio.sleep(0.05)
            report["push_applied"] = True
            report["entities"] = len(hass.states.async_all())
            if case == "reconnect":
                backend.push_available = False
                backend.power = "1"
                await backend.sockets[-1].close()
                while (
                    manager.schedulers[device].status.push_active
                    or coordinator.data[device]["power"] != "1"
                ):
                    await asyncio.sleep(0.05)
                report["tls_fallback"] = True
                backend.push_available = True
                while (
                    not manager.schedulers[device].status.push_active
                    or coordinator.data[device]["power"] != "0"
                ):
                    await asyncio.sleep(0.05)
                report["tls_reconnected"] = True
            if case == "sleep":
                coordinator.data[device].update(scenary="sleep", sleep_expired=True)
                coordinator.async_update_listeners()
                await supervisor.stopped.wait()
                return
            if real:
                await supervisor.stopped.wait()
                return
            # Exercise the internal API, not just HA's entity services.
            try:
                await entry.runtime_data.api.async_set_scenary(
                    "device", "occupied", retry=False
                )
            except PermissionError:
                report["internal_write_blocked"] = True

        task = asyncio.create_task(exercise())
        stopped = asyncio.create_task(supervisor.stopped.wait())
        try:
            done, _ = await asyncio.wait(
                {task, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                task.result()
            if not supervisor.stopped.is_set():
                raise RuntimeError("Synthetic operation did not stop the laboratory")
        except Exception:
            if case in {"tls", "redirect", "quota"} and supervisor.stopped.is_set():
                report["security_stop"] = True
            else:
                report["error"] = "scenario_failed"
        finally:
            if case in {"get", "authorization"}:
                report[f"inflight_{case}_at_deadline"] = bool(
                    gateway.protected.session.connector._acquired
                )
            if case == "cooldown":
                runtime = getattr(entry, "runtime_data", None)
                report["cooldown_pending_at_deadline"] = bool(
                    runtime and runtime.api._pending
                )
            task.cancel()
            stopped.cancel()
            watcher.cancel()
            await asyncio.gather(task, stopped, watcher, return_exceptions=True)
            try:
                if entry is not None:
                    if bucket := hass.data.get(entry.domain, {}).get(entry.entry_id):
                        if owner := bucket.get("sync_manager"):
                            await owner.close()
                            report["owned_tasks_closed"] = not owner._tasks and all(
                                s._task is None for s in owner.schedulers.values()
                            )
                    await hass.config_entries.async_unload(entry.entry_id)
                    if bucket := hass.data.get(entry.domain, {}).get(entry.entry_id):
                        if owner := bucket.get("sync_manager"):
                            await owner.close()
                    await hass.config_entries.async_remove(entry.entry_id)
            except Exception:
                report["error"] = "cleanup_failed"

        # Observe the shutdown owner before transport cleanup can mask pending I/O.
        report["pending_http_tasks"] = sum(
            not task.done()
            and getattr(task.get_coro(), "__qualname__", "")
            == "AirzoneAPI._authed_request_impl"
            for task in asyncio.all_tasks()
        )
        report["transports_open_when_checked"] = (
            not gateway.protected.session.closed
            and (backend is None or bool(backend.runner.sites))
        )

        async def close_backend():
            if backend is not None:
                if backend.wait:
                    backend.wait.set()
                await backend.close()

        def describe():
            return dict(
                reason=supervisor.reason,
                counts=supervisor.counts,
                operations=backend.operations if backend else None,
                cooldown_wait=case == "cooldown" and bool(report.get("loaded")),
                backend_control_writes=backend.controls if backend else None,
                session_closed=gateway.protected.session.closed,
                server_closed=not backend.runner.sites if backend else True,
                storage_files=len(list(Path(hass.config.path(".storage")).glob("*"))),
            )

        await finish_lab(
            hass, report, [gateway.protected.session.close, close_backend], describe
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, scenario)
    return True
