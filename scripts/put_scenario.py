"""Isolate a scoped PUT from this LAB's INFO/GET, then restore and read back."""

import asyncio
import time

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from scripts.put_trial import BoundedPutPolicy
from scripts.read_experiment import REQUIRED_SECONDS, observe_reads, snapshot_confirmed
from scripts.session_grant import approval_values, check_plan_approval, write_private
from scripts.setpoint_trial import SingleUseTrialJournal


async def execute_put_trial(
    hass,
    entry,
    manager,
    gateway,
    session,
    timeline,
    observations,
    report,
    plan,
    *,
    fixture,
):
    from custom_components.airzoneclouddaikin.push_adapter import PUSHER_APP_KEY

    directory, grant = session["directory"], session["grant"]
    values = {**approval_values(grant, plan), "approved": False}
    write_private(directory / "plan.json", values)
    if fixture:
        write_private(directory / "approval.json", {**values, "approved": True})
    while not check_plan_approval(directory / "approval.json", grant, plan):
        if gateway.supervisor.deadline - time.monotonic() < 120:
            gateway.supervisor.abort("insufficient_restoration_time")
        plan.check_device(manager.state.data[plan.device_id], restoring=False)
        await asyncio.sleep(0.1)
    journal = SingleUseTrialJournal(directory, nonce=grant["nonce"], plan=plan)
    policy = BoundedPutPolicy(
        plan,
        journal,
        lambda: manager.state.data[plan.device_id],
        PUSHER_APP_KEY,
        deadline=gateway.supervisor.deadline,
    )
    gateway.protected.policy = policy
    window = gateway.read_window
    experiment = grant.get("experiment")
    if experiment and (plan.field != "min_temp_unoccupied" or plan.power != "0"):
        journal.close()
        raise PermissionError("Read experiments require the OFF/Home minimum field")
    domain = "climate" if plan.field == "scenary" else "number"
    unique = (
        f"{plan.device_id}_climate"
        if domain == "climate"
        else f"{plan.device_id}_min_temp_unoccupied"
    )
    entity_id = er.async_get(hass).async_get_entity_id(domain, entry.domain, unique)
    if entity_id is None:
        journal.close()
        raise RuntimeError("PUT entity not available")

    @callback
    def published(event):
        state = event.data.get("new_state")
        if state is not None and state.entity_id == entity_id:
            value = (
                {"home": "occupied", "away": "vacant"}.get(
                    state.attributes.get("preset_mode")
                )
                if domain == "climate"
                else state.state
            )
            if value is not None:
                timeline.add("ha_published", field=plan.field, value=value, source="ha")

    remove_listener = hass.bus.async_listen("state_changed", published)

    async def put(value):
        timeline.add("control_invoked", field=plan.field, value=value, source="lab")
        data = {"entity_id": entity_id}
        if domain == "climate":
            service = "set_preset_mode"
            data["preset_mode"] = "away" if value == "vacant" else "home"
        else:
            service = "set_value"
            data["value"] = value
        await hass.services.async_call(domain, service, data, blocking=True)

    def push_seen(value, records):
        return any(
            x["kind"] == "pusher_received"
            and x.get("field") == plan.field
            and x.get("value", x.get("value_c")) == value
            for x in records
        )

    probe_task = None

    async def probe():
        nonlocal probe_task
        before = observations["get_applied"]
        probe_task = asyncio.create_task(manager.recover(plan.device_id))
        while observations["get_applied"] <= before:
            if probe_task.done():
                await probe_task
                raise RuntimeError("GET probe supplied no applied snapshot")
            await asyncio.sleep(0.05)

    async def confirmed_snapshot(restoring):
        # Do not mistake an optimistic/coordinator value for a received GET field.
        first = len(timeline.export())
        before = observations["get_applied"]
        await manager.recover(plan.device_id)
        expected = plan.modified if restoring else plan.original
        if (
            not snapshot_confirmed(
                timeline.export()[first:],
                plan.field,
                expected,
                state_value=manager.state.data[plan.device_id].get(plan.field),
            )
            or observations["get_applied"] <= before
        ):
            raise RuntimeError(
                "Fresh raw GET and raw manager state did not corroborate the expected value"
            )
        plan.check_device(manager.state.data[plan.device_id], restoring=restoring)
        return True

    try:
        await window.pause()  # Drain prior reads before starting the isolated window.
        required = REQUIRED_SECONDS[experiment] if experiment else 100
        if gateway.supervisor.deadline - time.monotonic() < required:
            gateway.supervisor.abort("insufficient_restoration_time")
        timeline.add("window_started", source="lab")
        start = len(timeline.export())
        policy.arm_change()
        await put(plan.modified)
        if experiment:
            await observe_reads(
                window,
                gateway,
                timeline,
                report,
                plan,
                experiment,
                probe,
                fixture=fixture and not session.get("fixture_wall_clock", False),
            )
        else:
            await asyncio.sleep(5 if fixture else 20)
            timeline.add("window_ended", source="lab")
            before_info = gateway.observations["information_accepted"]
            timeline.add("info_resumed", source="lab")
            window.resume()
            while gateway.observations["information_accepted"] < before_info + 2:
                await asyncio.sleep(0.05)
        records = timeline.export()
        end = next(
            i
            for i in range(start, len(records))
            if records[i]["kind"] == "window_ended"
        )
        isolated = records[start:end]
        report["put_without_lab_reads"] = not any(
            x["kind"] in {"info_admitted", "get_admitted"} for x in isolated
        )
        report["put_push_during_isolation"] = push_seen(plan.modified, isolated)
        report["put_push_after_resume"] = push_seen(plan.modified, records[end + 1 :])
        report["put_push_received"] = (
            report["put_push_during_isolation"] or report["put_push_after_resume"]
        )
        snapshot = await confirmed_snapshot(restoring=True)
        policy.arm_restoration(
            confirmed_by_push=report["put_push_received"],
            confirmed_by_snapshot=snapshot,
            owner_approved=True,
        )
        await put(plan.original)
        report["restoration_snapshot_confirmed"] = await confirmed_snapshot(
            restoring=False
        )
        report["put_field"] = plan.field
        report["put_payload"] = plan.exact_payload(restoring=False)
        report["session_passed"] = (
            report["put_without_lab_reads"]
            and report["restoration_snapshot_confirmed"]
            and not policy._revoked
        )
    finally:
        if window.paused("info") or window.paused("get"):
            timeline.add("window_ended", source="lab")
            timeline.add(
                "trial_stopped",
                source="lab",
                result="ambiguous" if policy._next_slot > 1 else "unavailable",
            )
            timeline.add("info_resumed", source="lab")
        report["needs_owner_intervention"] = policy._next_slot > 1 and not report.get(
            "restoration_snapshot_confirmed", False
        )
        window.resume()
        if probe_task is not None:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
        remove_listener()
        journal.close()
        report["trial_timeline"] = timeline.export()
