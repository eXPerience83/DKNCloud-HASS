"""Validate that the LAB timeline contains timing and provenance, never PII."""

import pytest

from scripts.trial_timeline import TrialTimeline


def test_timeline_captures_exact_source_order_with_monotonic_clock():
    now = iter([1000000000, 1000000001, 1800000000, 1900000000, 2000000000])
    timeline = TrialTimeline(clock=lambda: next(now))
    timeline.add("control_invoked", source="lab")
    timeline.add("info_admitted", source="transport")
    timeline.record_push_patch({"heat_consign": "21.0", "mac": "private"}, expected_field="heat_consign")
    timeline.add("get_applied", source="get", result="confirmed")
    records = timeline.export()
    assert [item["kind"] for item in records] == [
        "control_invoked",
        "info_admitted",
        "pusher_received",
        "get_applied",
    ]
    assert [item["elapsed_ms"] for item in records] == [
        0.000001,
        800.0,
        900.0,
        1000.0,
    ]
    assert records[2]["field"] == "heat_consign"
    assert records[2]["value_c"] == 21
    assert "private" not in str(records)
    assert "mac" not in str(records)


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        ("pusher_received", {"field": "mac", "value": "secret"}),
        ("pusher_received", {"field": "cold_consign", "value": "24.5"}),
        ("pusher_received", {"field": "heat_consign", "value": "nan"}),
        ("pusher_received", {"field": "heat_consign", "value": True}),
        ("pusher_received", {"value": 21}),
        ("arbitrary", {}),
        ("control_result", {"source": "my device"}),
        ("control_result", {"result": "HTTP body"}),
    ],
)
def test_timeline_rejects_unapproved_field_values_and_status(kind, options):
    timeline = TrialTimeline()
    with pytest.raises(ValueError):
        timeline.add(kind, **options)


def test_push_observation_ignores_unrelated_fields_and_export_is_immutable():
    timeline = TrialTimeline()
    timeline.record_push_patch({"power": "1", "mac": "secret"}, expected_field="cold_consign")
    assert timeline.export() == []
    timeline.record_push_patch(
        {"cold_consign": 25, "device_id": "hidden"}, expected_field="cold_consign"
    )
    observed = timeline.export()
    observed[0]["value_c"] = 99
    assert timeline.export()[0]["value_c"] == 25
