"""Fixed LAB-only INFO-first, GET-first and spontaneous observation phases."""

import asyncio

# Include supervisor spacing, two ordinary INFO cycles, successful-write
# verification, restoration, final verification and a cleanup margin.
REQUIRED_SECONDS = {"info-first": 175, "get-first": 215, "spontaneous": 210}


def target_push(records, field, value):
    return any(
        row["kind"] == "pusher_received"
        and row.get("field") == field
        and row.get("value", row.get("value_c")) == value
        for row in records
    )


def snapshot_confirmed(records, field, expected, *, state_value):
    """Corroborate raw cloud telemetry, including identical retained Pusher state."""
    received = [
        row.get("value", row.get("value_c"))
        for row in records
        if row["kind"] == "get_received" and row.get("field") == field
    ]
    if field != "scenary":
        from scripts.setpoint_trial import _whole

        state_value = _whole(state_value)
    return bool(received and received[-1] == state_value == expected)


async def observe_reads(
    window, gateway, timeline, report, plan, experiment, probe, *, fixture=False
):
    """Called after PUT ACK with both persistent gates paused and drained."""
    first = len(timeline.export())
    await asyncio.sleep(0.5 if fixture else 80 if experiment == "spontaneous" else 45)
    isolated = timeline.export()[first:]
    # The caller also checks the interval from window_started, including PUT I/O.
    if any(x["kind"] in {"info_admitted", "get_admitted"} for x in isolated):
        gateway.supervisor.abort("isolation_violation")
    timeline.add("window_ended", source="lab")
    report["experiment"] = experiment

    if experiment == "get-first":
        start = len(timeline.export())
        timeline.add("get_probe_started", source="lab")
        window.allow_once("get")
        await probe()
        await asyncio.sleep(0.5 if fixture else 20)
        timeline.add("get_probe_ended", source="lab")
        records = timeline.export()[start:]
        gets = [x for x in records if x["kind"] == "get_admitted"]
        raw = [
            x["value_c"]
            for x in records
            if x["kind"] == "get_received" and x.get("field") == plan.field
        ]
        report["get_first_raw_value"] = raw[-1] if raw else None
        report["get_first_push"] = target_push(records, plan.field, plan.modified)
        report["get_first_isolated"] = len(gets) == 1 and not any(
            x["kind"] == "info_admitted" for x in records
        )
        if not report["get_first_isolated"] or not window.paused("get"):
            gateway.supervisor.abort("single_get_violation")

    before_info = gateway.observations["information_accepted"]
    start = len(timeline.export())
    timeline.add("info_resumed", source="lab")
    window.resume("info")
    while gateway.observations["information_accepted"] < before_info + 2:
        await asyncio.sleep(0.05)
    # Give the second INFO response a finite event observation period.
    await asyncio.sleep(0.3 if fixture else 5)
    records = timeline.export()[start:]
    report["info_phase_push"] = target_push(records, plan.field, plan.modified)
    report["info_phase_without_get"] = not any(
        x["kind"] == "get_admitted" for x in records
    )
    if not report["info_phase_without_get"]:
        gateway.supervisor.abort("info_phase_get_violation")
    timeline.add("get_resumed", source="lab")
    window.resume("get")
