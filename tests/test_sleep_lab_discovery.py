"""The account discovery transport has no device command path."""

import pytest

from scripts.sleep_lab_discovery import DiscoveryPolicy
from scripts.sleep_lab_run import BASE


def test_discovery_policy_denies_device_commands_and_unlisted_sites():
    policy = DiscoveryPolicy()
    policy.installations = frozenset({"site"})
    auth = {"user_email": "fixture", "user_token": "fixture", "format": "json"}
    params = auth | {"installation_id": "site"}
    policy.validate("GET", BASE + "/devices", params=params)
    with pytest.raises(PermissionError):
        policy.validate(
            "GET", BASE + "/devices", params=auth | {"installation_id": "other"}
        )
    with pytest.raises(PermissionError):
        policy.validate("GET", BASE + "/devices", params=auth)
    with pytest.raises(PermissionError):
        policy.validate(
            "PUT", BASE + "/devices/unit", params=auth, json={"sleep_time": 5}
        )
    with pytest.raises(PermissionError):
        policy.validate("POST", BASE + "/events", json={"event": {"option": "P1"}})
    with pytest.raises(PermissionError):
        policy.validate_socket("wss://dkn.airzonecloud.com:8080")
