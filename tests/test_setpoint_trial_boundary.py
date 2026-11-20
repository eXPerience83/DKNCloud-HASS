"""Prove the existing LAB cannot run the proposed control trial as configured.

These are offline boundary checks, not an implementation or authorization of
the proposed two-command session and not evidence from a real DKN device.
"""

from unittest.mock import Mock

import pytest

from scripts.lab_policy import ProtectedSession, ReadOnlyPolicy
from scripts.protected_lab import LaboratoryGateway, LabStopped, LabSupervisor


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("P1", "0"),
        ("P2", "2"),
        ("P3", "1"),
        ("P4", "1"),
        ("P7", "25.0"),
        ("P7", "24.0"),
        ("P8", "21.0"),
        ("P8", "20.0"),
        ("P9", "1"),
        ("P10", "1"),
        ("P19", "1"),
        ("P20", "1"),
    ],
)
def test_canonical_controls_cannot_reach_transport(option, value):
    """Even a valid one-degree target or restoration is currently forbidden."""
    raw = Mock()
    policy = ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True)
    gateway = LaboratoryGateway(ProtectedSession(raw, policy), LabSupervisor())

    with pytest.raises(LabStopped):
        gateway.request(
            "POST",
            "https://dkn.airzonecloud.com/events",
            params={"user_email": "synthetic@example.invalid", "user_token": "fake"},
            json={
                "event": {
                    "cgi": "modmaquina",
                    "device_id": "device",
                    "option": option,
                    "value": value,
                }
            },
        )

    raw.request.assert_not_called()
    assert gateway.supervisor.reason == "forbidden_operation"
    assert gateway.observations["blocked_control_attempts"] == 1
    assert gateway.supervisor.counts == {"state": 0, "auth": 0}


@pytest.mark.parametrize(
    "payload",
    [
        {"device": {"scenary": "occupied"}},
        {"device": {"scenary": "sleep"}},
        {"sleep_time": 40},
        {"min_temp_unoccupied": 18},
        {"max_temp_unoccupied": 28},
    ],
)
def test_incidental_scene_or_number_write_cannot_reach_transport(payload):
    """Setup/cleanup side effects must meet the same boundary as entity calls."""
    raw = Mock()
    policy = ReadOnlyPolicy({"device"}, {"site"}, "synthetic", enabled=True)
    gateway = LaboratoryGateway(ProtectedSession(raw, policy), LabSupervisor())

    with pytest.raises(LabStopped):
        gateway.request(
            "PUT",
            "https://dkn.airzonecloud.com/devices/device",
            json=payload,
        )

    raw.request.assert_not_called()
    assert gateway.supervisor.reason == "forbidden_operation"
    assert gateway.observations["blocked_control_attempts"] == 1
