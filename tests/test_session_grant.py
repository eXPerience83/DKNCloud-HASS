"""Session authorization fails before any credential access or connection."""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.put_trial import PutPlan
from scripts.session_grant import (
    approval_values,
    check_plan_approval,
    claim_child,
    claim_session,
    source_digest,
    validate_grant,
    write_private,
)


def grant(root):
    (root / "pyproject.toml").write_text("synthetic")
    (root / "uv.lock").write_text("synthetic")
    return {
        "approved": True,
        "mode": "put",
        "field": "scenary",
        "nonce": "n" * 40,
        "device_id": "device",
        "installation_id": "site",
        "source_digest": source_digest(root),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }


@pytest.mark.parametrize(
    "change", ["unapproved", "expired", "source", "scope", "field"]
)
def test_invalid_session_grant_denied(tmp_path, change):
    value = grant(tmp_path)
    if change == "unapproved":
        value["approved"] = False
    elif change == "expired":
        value["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "source":
        (tmp_path / "uv.lock").write_text("changed")
    elif change == "scope":
        value["device_id"] = None
    else:
        value["field"] = "sleep_time"
    with pytest.raises(PermissionError):
        validate_grant(value, tmp_path)


def test_parent_and_child_permission_cannot_be_reused(tmp_path):
    value = grant(tmp_path)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    folder = claim_session(parent, validate_grant(value, tmp_path))
    claim_child(folder, value)
    with pytest.raises(FileExistsError):
        claim_child(folder, value)
    with pytest.raises(FileExistsError):
        claim_session(parent, value)


def test_exact_plan_approval_is_separate_from_session_grant(tmp_path):
    value = grant(tmp_path)
    plan = PutPlan("device", "site", "1", "scenary", "occupied", "vacant")
    path = tmp_path / "approval.json"
    assert not check_plan_approval(path, value, plan)
    approval = {**approval_values(value, plan), "approved": True}
    write_private(path, {**approval, "modified": "sleep"})
    with pytest.raises(PermissionError):
        check_plan_approval(path, value, plan)
    write_private(path, approval)
    assert check_plan_approval(path, value, plan)
    assert path.stat().st_mode & 0o777 == 0o600


def test_runner_refuses_unapproved_session_before_reading_credentials(
    tmp_path, monkeypatch
):
    import sys

    from scripts import run_supervised_ha

    value = grant(tmp_path)
    value["approved"] = False
    path = tmp_path / "grant.json"
    write_private(path, value)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--mode",
            "put",
            "--grant",
            str(path),
            "--session-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(
        sys.stdin,
        "readline",
        lambda: pytest.fail("Credential read before authorization"),
    )
    with pytest.raises(PermissionError):
        run_supervised_ha.main()
