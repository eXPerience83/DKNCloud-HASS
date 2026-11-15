"""One supervised run of the real integration through the TLS-only laboratory."""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import callback

from scripts.lab_lifecycle import finish_lab
from scripts.lab_policy import ReadOnlyPolicy
from scripts.lab_tls_backend import SyntheticBackend
from scripts.protected_lab import LabSupervisor, create_gateway, real_policy
from scripts.put_scenario import execute_put_trial
from scripts.put_trial import PutPlan
from scripts.session_grant import (
    ScopedDiscoveryPolicy,
    approval_values,
    claim_child,
    private_json,
    validate_grant,
    write_private,
)
from scripts.setpoint_trial import (
    BoundedSetpointPolicy,
    SetpointPlan,
    SingleUseTrialJournal,
)
from scripts.trial_timeline import TrialTimeline


async def async_setup(hass, config):
    import custom_components.airzoneclouddaikin as integration
    from custom_components.airzoneclouddaikin import config_flow
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
    from custom_components.airzoneclouddaikin.push_adapter import PUSHER_APP_KEY

    # No arbitrary integration log record may escape a future authenticated run.
    root = logging.getLogger()
    root.handlers = [logging.NullHandler()]
    session = None
    if session_path := os.environ.get("DKN_LAB_SESSION"):
        session = private_json(Path(session_path))
        from scripts import session_grant

        session["grant"] = validate_grant(
            session["grant"], Path(session_grant.__file__).resolve().parents[1]
        )
        session["directory"] = Path(session["directory"])
        claim_child(session["directory"], session["grant"])
    real = os.environ.get("DKN_LAB_TARGET") == "real"
    case = os.environ.get("DKN_LAB_CASE", "normal")
    if session is not None:
        grant = session["grant"]
        device, installation = grant["device_id"], grant["installation_id"]
        credentials = json.loads(await asyncio.to_thread(sys.stdin.readline))
        username, password = credentials.pop("email"), credentials.pop("password")
        credentials.clear()
        policy = ScopedDiscoveryPolicy(PUSHER_APP_KEY, grant)
        backend = None
        if session.get("synthetic") is True:
            backend = await SyntheticBackend(hass.config.config_dir).start()
            backend.trial_controls = grant["mode"] == "control"
            backend.event_kind = "setpoint_trial"
            if session.get("fixture_off"):
                backend.power = "0"
            if grant["mode"] == "put":
                backend.trial_put_field = grant["field"]
                backend.trial_push_on_info = session.get("push_on_info", False)
                backend.trial_stale_get = session.get("stale_get", False)
            os.environ["REQUESTS_CA_BUNDLE"] = str(
                Path(backend.directory) / "synthetic-cert.pem"
            )
        remaining = (
            datetime.fromisoformat(grant["expires_at"]) - datetime.now(UTC)
        ).total_seconds()
        supervisor = LabSupervisor(
            duration=min(285, remaining), auth_limit=2, socket_limit=1
        )
        gateway = await create_gateway(
            policy, supervisor, connector=backend.connector() if backend else None
        )
    elif real:
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
            if case in {"reconnect", "reconnect_no_events", "setpoint_trial"}
            else 60
            if case in {"normal", "sleep", "no_events", "invalid_event", "wrong_device"}
            else 35
            if case == "get"
            else 15
            if case in {"cooldown", "authorization"}
            else 3,
            state_limit=1 if case == "quota" else 40,
            spacing=0.01,
            socket_limit=1 if case == "setpoint_trial" else 6,
        )
        backend = await SyntheticBackend(hass.config.config_dir).start()
        # Trust only this disposable synthetic CA for the child HA process.
        # The real-service path never modifies the system trust store.
        os.environ["REQUESTS_CA_BUNDLE"] = str(
            Path(backend.directory) / "synthetic-cert.pem"
        )
        if case in {"duration", "authorization"}:
            backend.wait = asyncio.Event()
            backend.wait_path = {
                "authorization": "/pusher/auth",
                "duration": "/users/sign_in",
            }[case]
        backend.emit_events = case != "no_events"
        backend.event_kind = case
        backend.trial_controls = case == "setpoint_trial"
        backend.redirect = case == "redirect"
        policy = ReadOnlyPolicy({device}, {installation}, PUSHER_APP_KEY, enabled=True)
        gateway = await create_gateway(
            policy, supervisor, connector=backend.connector(trusted=case != "tls")
        )

    timeline = (
        TrialTimeline() if case == "setpoint_trial" or session is not None else None
    )
    gateway.timeline = timeline
    from custom_components.airzoneclouddaikin.sync_manager import SyncManager

    observations = dict(
        device_received=0,
        device_changed_state=0,
        entity_observed_push=0,
        device_applied=0,
        get_applied=0,
        subscribed=False,
        last_device_age=None,
    )
    original_event = SyncManager.event
    original_read = SyncManager._read
    original_subscription = SyncManager.subscription
    frozen_plan = None
    remove_state_listener = None
    from custom_components.airzoneclouddaikin import climate, number

    original_optimistic = climate.optimistic_set
    original_number_optimistic = number.optimistic_set

    def observed_optimistic(*args, **kwargs):
        result = original_optimistic(*args, **kwargs)
        if (
            timeline is not None
            and len(args) >= 5
            and args[3]
            in {"cold_consign", "heat_consign", "scenary", "min_temp_unoccupied"}
        ):
            timeline.add("ha_optimistic", field=args[3], value=args[4], source="ha")
        return result

    if timeline is not None:
        climate.optimistic_set = observed_optimistic
        number.optimistic_set = observed_optimistic

    async def observed_event(manager, identity, patch, generation):
        if (
            timeline is not None
            and identity in gateway.protected.policy.devices
            and not manager._stopped
            and not manager._auth_failed
            and generation == manager.state.generation
            and ("id" not in patch or str(patch["id"]) == identity)
        ):
            for field in (
                "cold_consign",
                "heat_consign",
                "min_temp_unoccupied",
                "scenary",
            ):
                timeline.record_push_patch(patch, expected_field=field)
            if frozen_plan is not None and (
                (
                    "power" in patch
                    and str(patch["power"]) != getattr(frozen_plan, "power", "1")
                )
                or ("mode" in patch and str(patch["mode"]) != frozen_plan.mode)
                or (
                    "scenary" in patch
                    and patch["scenary"]
                    not in (
                        {frozen_plan.original, frozen_plan.modified}
                        if frozen_plan.field == "scenary"
                        else {"occupied"}
                    )
                )
            ):
                supervisor.abort("unexpected_trial_state")
        previous = dict(manager.state.data.get(identity, {}))
        revision = manager.state.revision
        await original_event(manager, identity, patch, generation)
        if manager.state.revision != revision:
            observations["device_received"] += 1
            if any(previous.get(k) != v for k, v in patch.items() if v is not None):
                observations["device_changed_state"] += 1
            state = manager.state.data[identity]
            coordinator = hass.data["airzoneclouddaikin"][entry_id[0]]["coordinator"]
            if all(
                coordinator.data[identity].get(k) == v == state.get(k)
                for k, v in patch.items()
                if v is not None
            ):
                observations["device_applied"] += 1
                if timeline is not None:
                    timeline.add("coordinator_applied", source="pusher")
                if patch.get("power") == "0" and any(
                    entity.domain == "climate" and entity.state == "off"
                    for entity in hass.states.async_all()
                ):
                    observations["entity_observed_push"] += 1
                observations["last_device_age"] = time.monotonic()

    async def observed_read(manager, *args, **kwargs):
        result = await original_read(manager, *args, **kwargs)
        if timeline is not None:
            if frozen_plan is not None and frozen_plan.field in manager.touched.get(
                device, set()
            ):
                timeline.add(
                    "get_applied",
                    field=frozen_plan.field,
                    value=manager.state.data[device][frozen_plan.field],
                    source="get",
                )
            else:
                timeline.add("get_applied", source="get")
        observations["get_applied"] += 1
        return result

    async def observed_subscription(manager, identity, active):
        if active:
            if timeline is not None:
                timeline.add("subscription_ack", source="pusher")
            observations["subscribed"] = True
        elif (
            session is not None and observations["subscribed"] and not manager._stopped
        ):
            supervisor.abort("push_disconnected")
        return await original_subscription(manager, identity, active)

    entry_id = [None]
    SyncManager.event = observed_event
    SyncManager._read = observed_read
    SyncManager.subscription = observed_subscription
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
        SyncManager.event = original_event
        SyncManager._read = original_read
        SyncManager.subscription = original_subscription
        climate.optimistic_set = original_optimistic
        number.optimistic_set = original_number_optimistic
        Store.async_save, Store.async_delay_save = original_save, original_delay
        integration.async_get_clientsession = original
        config_flow.async_get_clientsession = original_flow
        if remove_state_listener is not None:
            remove_state_listener()

    hass.data["lab_restore_guard"] = restore_guard

    async def scenario(_):
        report = {"real": backend is None and (session is not None or real)}
        if session is not None:
            report["source_digest"] = grant["source_digest"]
        entry = None
        trial_journal = None
        watcher = asyncio.create_task(supervisor.watch())

        async def exercise():
            nonlocal \
                entry, \
                trial_journal, \
                device, \
                installation, \
                frozen_plan, \
                remove_state_listener
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
            entry_id[0] = entry.entry_id
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
            if session is not None:
                device = next(iter(gateway.protected.policy.devices))
                installation = next(iter(gateway.protected.policy.installations))
                write_private(
                    session["directory"] / "scope.json",
                    {"device_id": device, "installation_id": installation},
                )
            while not manager.schedulers[device].status.push_active:
                await asyncio.sleep(0.05)
            while not all(
                any(s.domain == domain for s in hass.states.async_all())
                for domain in {"climate", "switch", "number", "sensor", "binary_sensor"}
            ):
                await asyncio.sleep(0.05)
            if case in {"no_events", "invalid_event", "wrong_device"}:
                await supervisor.stopped.wait()
                return
            while not observations["device_applied"]:
                await asyncio.sleep(0.05)
            report["push_applied"] = True
            report["entities"] = len(hass.states.async_all())
            if session is not None:
                # Private preflight facts needed to assess the effects of Away.
                current = manager.state.data[device]
                write_private(
                    session["directory"] / "baseline.json",
                    {
                        key: current.get(key)
                        for key in (
                            "power",
                            "mode",
                            "scenary",
                            "effective_scenary",
                            "cold_consign",
                            "heat_consign",
                            "min_temp_unoccupied",
                            "max_temp_unoccupied",
                            "min_limit_cold",
                            "max_limit_cold",
                            "min_limit_heat",
                            "max_limit_heat",
                        )
                    },
                )
            if session is not None and grant["mode"] == "put":
                frozen_plan = PutPlan.from_device(
                    manager.state.data[device],
                    device_id=device,
                    installation_id=installation,
                    field=grant["field"],
                )
                await execute_put_trial(
                    hass,
                    entry,
                    manager,
                    gateway,
                    session,
                    timeline,
                    observations,
                    report,
                    frozen_plan,
                    fixture=backend is not None,
                )
                supervisor.reason = "put_finished"
                supervisor.stopped.set()
                return
            if timeline is not None:

                @callback
                def state_published(event):
                    state = event.data.get("new_state")
                    if (
                        state is not None
                        and state.domain == "climate"
                        and state.attributes.get("temperature") is not None
                    ):
                        field = (
                            "heat_consign" if state.state == "heat" else "cold_consign"
                        )
                        timeline.add(
                            "ha_published",
                            field=field,
                            value=state.attributes["temperature"],
                            source="ha",
                        )

                remove_state_listener = hass.bus.async_listen(
                    "state_changed", state_published
                )
            if session is not None and grant["mode"] == "observe":
                before_info = gateway.observations["information_accepted"]
                while gateway.observations["information_accepted"] < before_info + 2:
                    await asyncio.sleep(0.05)
                put_plans = []
                for field in ("scenary", "min_temp_unoccupied"):
                    try:
                        candidate = PutPlan.from_device(
                            manager.state.data[device],
                            device_id=device,
                            installation_id=installation,
                            field=field,
                        )
                        put_plans.append(
                            {**approval_values(grant, candidate), "approved": False}
                        )
                    except ValueError:
                        pass
                write_private(
                    session["directory"] / "put-plans.json", {"candidates": put_plans}
                )
                report["session_passed"] = True
                report["trial_timeline"] = timeline.export()
                supervisor.reason = "observation_finished"
                supervisor.stopped.set()
                return
            if case == "setpoint_trial":
                # Exercise the authentic HA Climate service and transport while
                # targeting ONLY the local TLS fixture. This is not a real-run
                # authorization path; the global live gate remains disabled.
                current = manager.state.data[device]
                frozen = SetpointPlan.from_device(
                    current, device_id=device, installation_id=installation
                )
                frozen_plan = frozen
                private = Path(hass.config.config_dir) / "trial-private"
                private.mkdir(mode=0o700)
                trial_journal = SingleUseTrialJournal(
                    private, nonce="synthetic-session-" + "s" * 40, plan=frozen
                )
                policy = BoundedSetpointPolicy(
                    frozen,
                    trial_journal,
                    lambda: manager.state.data[device],
                    PUSHER_APP_KEY,
                    deadline=supervisor.deadline,
                )
                gateway.protected.policy = policy
                entity_id = next(
                    state.entity_id
                    for state in hass.states.async_all()
                    if state.domain == "climate"
                )

                async def set_temperature(value):
                    timeline.add("control_invoked", source="lab")
                    await hass.services.async_call(
                        "climate",
                        "set_temperature",
                        {"entity_id": entity_id, "temperature": value},
                        blocking=True,
                    )

                policy.arm_change()
                before_info = gateway.observations["information_accepted"]
                first_event = len(timeline.export())
                await set_temperature(frozen.modified)
                before_info = gateway.observations["information_accepted"]

                def observed_push(value, after):
                    return any(
                        item["kind"] == "pusher_received"
                        and item.get("field") == frozen.field
                        and item.get("value_c") == value
                        for item in timeline.export()[after:]
                    )

                for _ in range(650):
                    if (
                        float(manager.state.data[device].get(frozen.field))
                        == frozen.modified
                        and observed_push(frozen.modified, first_event)
                        and gateway.observations["information_accepted"]
                        >= before_info + 2
                    ):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError(
                        "Synthetic setpoint not confirmed by Pusher/INFO"
                    )

                policy.arm_restoration(confirmed_by_push=True, owner_approved=True)
                restored_after = len(timeline.export())
                await set_temperature(frozen.original)
                for _ in range(100):
                    if float(
                        manager.state.data[device].get(frozen.field)
                    ) == frozen.original and observed_push(
                        frozen.original, restored_after
                    ):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("Synthetic restoration not confirmed")

                report["trial_passed"] = (
                    backend.controls == 2
                    and str(backend.cold_consign) == f"{frozen.original}.0"
                    and not policy._revoked
                    and len(
                        [
                            event
                            for event in timeline.export()
                            if event["kind"] == "control_admitted"
                        ]
                    )
                    == 2
                )
                report["trial_timeline"] = timeline.export()
                supervisor.reason = "trial_finished"
                supervisor.stopped.set()
                return
            if case == "get":
                before = backend.operations.get("get", 0)
                backend.wait = asyncio.Event()
                backend.wait_path = "/devices"
                manager.request_recovery(device)
                while backend.operations.get("get", 0) <= before:
                    await asyncio.sleep(0.05)
                report["pending_recovery_get_reached"] = True
                await supervisor.stopped.wait()
                return
            if case in {"reconnect", "reconnect_no_events"}:
                before_events = observations["device_applied"]
                backend.push_available = False
                backend.power = "1"
                await backend.sockets[-1].close()
                while (
                    manager.schedulers[device].status.push_active
                    or coordinator.data[device]["power"] != "1"
                ):
                    await asyncio.sleep(0.05)
                report["tls_fallback"] = True
                backend.emit_events = case != "reconnect_no_events"
                backend.push_available = True
                while (
                    not manager.schedulers[device].status.push_active
                    or coordinator.data[device]["power"] != "0"
                ):
                    await asyncio.sleep(0.05)
                report["tls_reconnected"] = True
                if case == "reconnect_no_events":
                    await supervisor.stopped.wait()
                report["push_recovered"] = (
                    observations["device_applied"] > before_events
                )
            if case == "sleep":
                # Inject the post-confirmation state, not merely a timed-out
                # Sleep scene. The write barrier must reject the resulting
                # automatic Home command even when OFF was corroborated.
                tracking = hass.data[entry.domain][entry.entry_id]["sleep_tracking"][
                    device
                ]
                tracking.last_scenary = "sleep"
                tracking.sleep_started_at_utc = integration.dt_util.utcnow()
                tracking.observed_on = True
                tracking.consecutive_off_reports = 2
                coordinator.data[device].update(
                    scenary="sleep", power="0", sleep_off_confirmed=True
                )
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
        except asyncio.CancelledError:
            report["error"] = "scenario_cancelled"
            raise
        except Exception:
            if case in {"tls", "redirect", "quota"} and supervisor.stopped.is_set():
                report["security_stop"] = True
            else:
                report["error"] = "scenario_failed"
        finally:
            try:
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
                if trial_journal is not None:
                    trial_journal.close()
                if case == "get":
                    runtime = getattr(entry, "runtime_data", None)
                    report["get_pending_before_owner_shutdown"] = bool(
                        runtime
                        and runtime.api._pending
                        and gateway.protected.session.connector._acquired
                    )
                try:
                    if entry is not None:
                        if bucket := hass.data.get(entry.domain, {}).get(
                            entry.entry_id
                        ):
                            if owner := bucket.get("sync_manager"):
                                await owner.close()
                                report["owned_tasks_closed"] = not owner._tasks and all(
                                    s._task is None for s in owner.schedulers.values()
                                )
                        await hass.config_entries.async_unload(entry.entry_id)
                        if bucket := hass.data.get(entry.domain, {}).get(
                            entry.entry_id
                        ):
                            if owner := bucket.get("sync_manager"):
                                await owner.close()
                        await hass.config_entries.async_remove(entry.entry_id)
                except Exception:
                    report["error"] = "cleanup_failed"

            finally:
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
                    if timeline is not None:
                        report["trial_timeline"] = timeline.export()
                    last = observations["last_device_age"]
                    observations["last_device_age"] = (
                        time.monotonic() - last if last else None
                    )
                    return dict(
                        observations=observations | gateway.observations,
                        pusher_result="verified"
                        if observations["device_applied"]
                        else "inconclusive",
                        safely_finished=report.get("pending_http_tasks") == 0
                        and gateway.protected.session.closed,
                        reason=supervisor.reason,
                        counts=supervisor.counts,
                        operations=backend.operations if backend else None,
                        cooldown_wait=case == "cooldown" and bool(report.get("loaded")),
                        backend_control_writes=backend.controls if backend else None,
                        session_closed=gateway.protected.session.closed,
                        server_closed=not backend.runner.sites if backend else True,
                        storage_files=len(
                            list(Path(hass.config.path(".storage")).glob("*"))
                        ),
                    )

                await finish_lab(
                    hass,
                    report,
                    [gateway.protected.session.close, close_backend],
                    describe,
                )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, scenario)
    return True
