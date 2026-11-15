"""Offline contract tests for the proposed two-command DKN setpoint trial.

These never start a live laboratory or authorize real destinations.
"""

import asyncio
import json
import stat

import pytest

from scripts.lab_policy import ProtectedSession
from scripts.protected_lab import LaboratoryGateway, LabStopped, LabSupervisor
from scripts.setpoint_trial import (
    BoundedSetpointPolicy,
    SetpointPlan,
    SingleUseTrialJournal,
)

ENDPOINT = "https://dkn.airzonecloud.com/events"
AUTH = {"user_email": "synthetic@example.invalid", "user_token": "fake"}


def device(mode="1", setpoint="24", **updates):
    value = {
        "id": "device",
        "power": "1",
        "scenary": "occupied",
        "mode": mode,
        "cold_consign": setpoint,
        "heat_consign": setpoint,
        "min_limit_cold": "16.0",
        "max_limit_cold": "32.0",
        "min_limit_heat": "16.0",
        "max_limit_heat": "32.0",
    }
    value.update(updates)
    return value


def plan(raw):
    return SetpointPlan.from_device(raw, device_id="device", installation_id="site")


def journal(tmp_path, frozen, nonce="a" * 32):
    root = tmp_path / "private"
    root.mkdir(mode=0o700, exist_ok=True)
    return SingleUseTrialJournal(root, nonce=nonce, plan=frozen)


class _Reply:
    def __init__(self, status=201):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _NoNetwork:
    def __init__(self):
        self.writes = []
        self.status = 201

    def request(self, method, url, **kwargs):
        self.writes.append((method, url, kwargs))
        return _Reply(self.status)


def gateway(raw, tmp_path):
    frozen = plan(raw)
    book = journal(tmp_path, frozen)
    policy = BoundedSetpointPolicy(
        frozen,
        book,
        lambda: raw,
        "synthetic",
        deadline=asyncio.get_running_loop().time() + 285,
    )
    network = _NoNetwork()
    supervisor = LabSupervisor(
        duration=285, state_limit=40, auth_limit=2, spacing=0, socket_limit=1
    )
    protected = ProtectedSession(network, policy)
    return LaboratoryGateway(protected, supervisor), policy, book, network


@pytest.mark.parametrize(
    ("mode", "setpoint", "max_limit", "option", "modified"),
    [
        ("1", "24", "32", "P7", 25),
        ("2", "20", "32", "P8", 21),
        ("1", "32", "32", "P7", 31),
        ("2", "16", "32", "P8", 17),
    ],
)
def test_frozen_plan_uses_one_celsius_step_and_actual_limits(
    mode, setpoint, max_limit, option, modified
):
    raw = device(mode=mode, setpoint=setpoint)
    raw["max_limit_cold" if mode == "1" else "max_limit_heat"] = max_limit
    frozen = plan(raw)
    assert frozen.option == option
    assert frozen.modified == modified
    assert abs(frozen.modified - frozen.original) == 1
    assert frozen.exact_payload(restoring=False)["event"]["value"] == f"{modified}.0"


@pytest.mark.parametrize(
    "updates",
    [
        {"power": "0"},
        {"scenary": "away"},
        {"scenary": "sleep"},
        {"mode": "3"},
        {"mode": "8"},
        {"mode": "4"},
        {"cold_consign": "24.5"},
        {"cold_consign": None},
        {"min_limit_cold": None},
        {"max_limit_cold": None},
        {"min_limit_cold": "nan"},
        {"min_limit_cold": "25", "max_limit_cold": "23"},
        {"min_limit_cold": "24", "max_limit_cold": "24"},
    ],
)
def test_unsupported_raw_state_has_no_plan(updates):
    with pytest.raises(ValueError):
        plan(device(**updates))


def test_journal_is_exclusive_durable_and_private(tmp_path):
    frozen = plan(device())
    book = journal(tmp_path, frozen)
    assert stat.S_IMODE(book.path.stat().st_mode) == 0o600
    book.consume(1, option="P7", value="25.0")
    file = book.path
    book.close()
    records = [json.loads(x) for x in file.read_text().splitlines()]
    assert [x["kind"] for x in records] == ["claim", "attempt"]
    assert records[-1]["slot"] == 1
    assert "fake" not in file.read_text()
    with pytest.raises(FileExistsError):
        SingleUseTrialJournal(file.parent, nonce="a" * 32, plan=frozen)


@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_journal_directory_requires_private_permissions(tmp_path, mode):
    root = tmp_path / "shared"
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(PermissionError):
        SingleUseTrialJournal(root, nonce="a" * 32, plan=plan(device()))


@pytest.mark.asyncio
async def test_transport_allows_only_frozen_change_and_approved_restore(tmp_path):
    raw = device()
    gateway_obj, policy, book, network = gateway(raw, tmp_path)
    payload1 = policy.plan.exact_payload(restoring=False)
    payload2 = policy.plan.exact_payload(restoring=True)
    policy.arm_change()
    async with gateway_obj.request("POST", ENDPOINT, params=AUTH, json=payload1):
        pass
    assert len(network.writes) == 1
    assert network.writes[0][2]["json"] == payload1

    raw["cold_consign"] = "25"
    with pytest.raises(PermissionError):
        policy.arm_restoration(confirmed_by_push=False, owner_approved=True)
    policy.arm_restoration(confirmed_by_push=True, owner_approved=True)
    async with gateway_obj.request("POST", ENDPOINT, params=AUTH, json=payload2):
        pass
    assert len(network.writes) == 2
    assert network.writes[1][2]["json"] == payload2

    with pytest.raises(LabStopped):
        async with gateway_obj.request("POST", ENDPOINT, params=AUTH, json=payload2):
            pass
    assert len(network.writes) == 2
    book.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option,value", [("P1", "1"), ("P2", "2"), ("P3", "1"), ("P8", "23.0"),
                     ("P7", "26.0"), ("P7", "24.0")]
)
async def test_other_controls_are_denied_before_network(tmp_path, option, value):
    raw = device()
    gateway_obj, policy, book, network = gateway(raw, tmp_path)
    policy.arm_change()
    request = policy.plan.exact_payload(restoring=False)
    request["event"].update(option=option, value=value)
    with pytest.raises(LabStopped):
        async with gateway_obj.request("POST", ENDPOINT, params=AUTH, json=request):
            pass
    assert network.writes == []
    assert book.path.read_text().count('"kind":"attempt"') == 0
    book.close()


@pytest.mark.asyncio
async def test_payload_freezes_before_budget_wait_and_revalidates_live_state(tmp_path):
    raw = device()
    gateway_obj, policy, book, network = gateway(raw, tmp_path)
    policy.arm_change()
    payload = policy.plan.exact_payload(restoring=False)
    pending = gateway_obj.request("POST", ENDPOINT, params=AUTH, json=payload)
    payload["event"]["value"] = "1.0"
    async with pending:
        pass
    assert network.writes[0][2]["json"]["event"]["value"] == "25.0"
    book.close()

    other_root = tmp_path / "other"
    other_root.mkdir()
    raw2 = device()
    gateway2, policy2, book2, network2 = gateway(raw2, other_root)
    policy2.arm_change()
    waiting = gateway2.request(
        "POST", ENDPOINT, params=AUTH, json=policy2.plan.exact_payload(restoring=False)
    )
    raw2["mode"] = "2"
    with pytest.raises(PermissionError):
        async with waiting:
            pass
    assert network2.writes == []
    assert book2.path.read_text().count('"kind":"attempt"') == 0
    book2.close()


@pytest.mark.asyncio
async def test_http_429_stops_before_any_second_wire_attempt(tmp_path):
    raw = device()
    gateway_obj, policy, book, network = gateway(raw, tmp_path)
    policy.arm_change()
    network.status = 429
    with pytest.raises(LabStopped):
        async with gateway_obj.request(
            "POST",
            ENDPOINT,
            params=AUTH,
            json=policy.plan.exact_payload(restoring=False),
        ):
            pass
    assert gateway_obj.supervisor.reason == "http_security"
    assert len(network.writes) == 1
    assert book.path.read_text().count('"kind":"attempt"') == 1
    book.close()


@pytest.mark.asyncio
async def test_concurrent_control_entries_consume_only_first_attempt(tmp_path):
    raw = device()
    gateway_obj, policy, book, network = gateway(raw, tmp_path)
    policy.arm_change()
    request = policy.plan.exact_payload(restoring=False)

    async def send():
        try:
            async with gateway_obj.request("POST", ENDPOINT, params=AUTH, json=request):
                return True
        except (LabStopped, PermissionError):
            return False

    outcomes = await asyncio.gather(send(), send())
    assert outcomes.count(True) == 1
    assert len(network.writes) == 1
    assert book.path.read_text().count('"kind":"attempt"') == 1
    book.close()


@pytest.mark.asyncio
async def test_restoration_rejected_after_competing_device_change(tmp_path):
    raw = device()
    gateway_obj, policy, book, network = gateway(raw, tmp_path)
    policy.arm_change()
    async with gateway_obj.request(
        "POST",
        ENDPOINT,
        params=AUTH,
        json=policy.plan.exact_payload(restoring=False),
    ):
        pass
    raw["cold_consign"] = "25"
    raw["scenary"] = "vacant"
    with pytest.raises(ValueError):
        policy.arm_restoration(confirmed_by_push=True, owner_approved=True)
    assert len(network.writes) == 1
    book.close()
