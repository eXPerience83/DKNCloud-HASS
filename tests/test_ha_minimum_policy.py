"""Independent fixed Home Assistant minimum compatibility policy tests."""

from __future__ import annotations

import pytest

from scripts.ha_minimum_policy import MinimumPolicyError, plan_minimum


def _project(ha: str = "2026.11.0", official: str = "2026.11.0") -> dict:
    return {
        "dependency-groups": {
            "test": [
                f"homeassistant=={ha}",
                f"pytest-homeassistant=={official}",
                "pytest==9.0.3",
            ]
        }
    }


def test_exact_current_minimum_skips_duplicate_full_suite() -> None:
    result = plan_minimum(_project(), {"homeassistant": "2026.11.0"})
    assert result.minimum_homeassistant == "2026.11.0"
    assert result.current_homeassistant == "2026.11.0"
    assert result.run_minimum is False


def test_newer_current_baseline_requires_independent_minimum_suite() -> None:
    result = plan_minimum(
        _project("2026.12.0", "2026.12.0"),
        {"homeassistant": "2026.11.0"},
    )
    assert result.run_minimum is True


@pytest.mark.parametrize(
    ("ha", "official"),
    [
        ("2026.10.4", "2026.10.4"),
        ("2026.12.0", "2026.11.0"),
        ("2026.12.0b1", "2026.12.0b1"),
    ],
)
def test_rejects_downgrade_mismatch_and_prerelease(ha: str, official: str) -> None:
    with pytest.raises(MinimumPolicyError):
        plan_minimum(_project(ha, official), {"homeassistant": "2026.11.0"})


def test_rejects_accidental_support_floor_bump() -> None:
    with pytest.raises(MinimumPolicyError, match="floor"):
        plan_minimum(_project(), {"homeassistant": "2026.12.0"})


def test_rejects_legacy_phacc() -> None:
    p = _project()
    p["dependency-groups"]["test"].append("pytest-homeassistant-custom-component==0.13.375")
    with pytest.raises(MinimumPolicyError, match="PHACC"):
        plan_minimum(p, {"homeassistant": "2026.11.0"})
