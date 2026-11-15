"""Isolate a scoped PUT from this LAB's INFO/GET, then restore and read back."""

import asyncio
import time

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from scripts.put_trial import BoundedPutPolicy, ReadWindow
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
    window = ReadWindow()
    gateway.read_window = window
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

    try:
        await window.pause()  # Drain prior reads before starting the isolated window.
        if gateway.supervisor.deadline - time.monotonic() < 100:
            gateway.supervisor.abort("insufficient_restoration_time")
        timeline.add("window_started", source="lab")
        start = len(timeline.export())
        policy.arm_change()
        await put(plan.modified)
        await asyncio.sleep(5 if fixture else 20)
        isolated = timeline.export()[start:]
        report["put_without_lab_reads"] = not any(
            x["kind"] in {"info_admitted", "get_admitted"} for x in isolated
        )
        report["put_push_during_isolation"] = push_seen(plan.modified, isolated)
        timeline.add("window_ended", source="lab")
        before_info = gateway.observations["information_accepted"]
        timeline.add("info_resumed", source="lab")
        resumed_at = len(timeline.export())
        window.resume()
        while gateway.observations["information_accepted"] < before_info + 2:
            await asyncio.sleep(0.05)
        report["put_push_after_resume"] = push_seen(
            plan.modified, timeline.export()[resumed_at:]
        )
        report["put_push_received"] = (
            report["put_push_during_isolation"] or report["put_push_after_resume"]
        )
        before_get = observations["get_applied"]
        await manager.recover(plan.device_id)
        snapshot = observations["get_applied"] > before_get
        plan.check_device(manager.state.data[plan.device_id], restoring=True)
        policy.arm_restoration(
            confirmed_by_push=report["put_push_received"],
            confirmed_by_snapshot=snapshot,
            owner_approved=True,
        )
        await put(plan.original)
        before_get = observations["get_applied"]
        await manager.recover(plan.device_id)
        plan.check_device(manager.state.data[plan.device_id], restoring=False)
        report["restoration_snapshot_confirmed"] = (
            observations["get_applied"] > before_get
        )
        report["put_field"] = plan.field
        report["put_payload"] = plan.exact_payload(restoring=False)
        report["session_passed"] = (
            report["put_without_lab_reads"]
            and report["restoration_snapshot_confirmed"]
            and not policy._revoked
        )
    finally:
        if not window.open.is_set():
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
        remove_listener()
        journal.close()
        report["trial_timeline"] = timeline.export()
