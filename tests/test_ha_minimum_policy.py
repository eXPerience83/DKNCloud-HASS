"""Independent fixed Home Assistant minimum compatibility policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ha_minimum_policy import (
    MinimumPolicyError,
    plan_minimum,
    verify_frozen_minimum_inputs,
)


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


def test_exact_current_minimum_still_runs_independent_hash_locked_suite() -> None:
    result = plan_minimum(_project(), {"homeassistant": "2026.11.0"})
    assert result.minimum_homeassistant == "2026.11.0"
    assert result.current_homeassistant == "2026.11.0"
    assert result.run_minimum is True


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
    p["dependency-groups"]["test"].append(
        "pytest-homeassistant-custom-component==0.13.375"
    )
    with pytest.raises(MinimumPolicyError, match="PHACC"):
        plan_minimum(p, {"homeassistant": "2026.11.0"})


# Use synthetic, offline files: the production hash lock is deliberately not
# rewritten during regression tests.
_FROZEN_PINS = {
    "homeassistant": "2026.11.0",
    "pytest-homeassistant": "2026.11.0",
    "pytest": "9.0.3",
    "pytest-cov": "7.1.0",
    "aiointercept": "1.0.0",
    "packaging": "26.3",
}


def _compiled_record(name: str, version: str) -> str:
    return f"{name}=={version} {chr(92)}\n    --hash=sha256:{'0' * 64}\n"


@pytest.fixture
def frozen_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    requirements = tmp_path / ".github" / "requirements"
    requirements.mkdir(parents=True)
    input_file = requirements / "minimum-ha.in"
    lock_file = requirements / "minimum-ha.txt"
    input_file.write_text(
        "".join(f"{name}=={version}\n" for name, version in _FROZEN_PINS.items()),
        encoding="utf-8",
    )
    lock_file.write_text(
        "".join(
            _compiled_record(name, version) for name, version in _FROZEN_PINS.items()
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return input_file, lock_file


def test_valid_frozen_minimum_inputs(frozen_files: tuple[Path, Path]) -> None:
    verify_frozen_minimum_inputs()


def test_rejects_duplicate_frozen_pin(frozen_files: tuple[Path, Path]) -> None:
    input_file, _ = frozen_files
    input_file.write_text(
        input_file.read_text(encoding="utf-8") + "homeassistant==2026.11.0\n",
        encoding="utf-8",
    )
    with pytest.raises(MinimumPolicyError, match="Duplicate fixed minimum input"):
        verify_frozen_minimum_inputs()


def test_rejects_non_exact_frozen_pin(frozen_files: tuple[Path, Path]) -> None:
    input_file, _ = frozen_files
    input_file.write_text(
        input_file.read_text(encoding="utf-8").replace(
            "pytest==9.0.3", "pytest>=9.0.3"
        ),
        encoding="utf-8",
    )
    with pytest.raises(MinimumPolicyError, match="Invalid fixed minimum input"):
        verify_frozen_minimum_inputs()


def test_rejects_drifted_frozen_pin(frozen_files: tuple[Path, Path]) -> None:
    input_file, _ = frozen_files
    input_file.write_text(
        input_file.read_text(encoding="utf-8").replace(
            "packaging==26.3", "packaging==26.4"
        ),
        encoding="utf-8",
    )
    with pytest.raises(MinimumPolicyError, match="Minimum input pins must match"):
        verify_frozen_minimum_inputs()


def test_rejects_missing_lock_record(frozen_files: tuple[Path, Path]) -> None:
    _, lock_file = frozen_files
    lock_file.write_text(
        lock_file.read_text(encoding="utf-8").replace(
            _compiled_record("pytest", "9.0.3"), ""
        ),
        encoding="utf-8",
    )
    with pytest.raises(MinimumPolicyError, match="Missing frozen minimum lock record"):
        verify_frozen_minimum_inputs()


def test_rejects_homeassistant_suffix_false_positive(
    frozen_files: tuple[Path, Path],
) -> None:
    _, lock_file = frozen_files
    lock_file.write_text(
        lock_file.read_text(encoding="utf-8").replace(
            _compiled_record("homeassistant", "2026.11.0"), "", 1
        ),
        encoding="utf-8",
    )
    assert "pytest-homeassistant==2026.11.0" in lock_file.read_text(encoding="utf-8")
    with pytest.raises(MinimumPolicyError, match="homeassistant"):
        verify_frozen_minimum_inputs()


def test_rejects_hashless_lock(frozen_files: tuple[Path, Path]) -> None:
    _, lock_file = frozen_files
    lock_file.write_text(
        "\n".join(
            line
            for line in lock_file.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--hash=sha256:")
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MinimumPolicyError, match="SHA-256 hash"):
        verify_frozen_minimum_inputs()


def test_rejects_hash_missing_for_single_pin(frozen_files: tuple[Path, Path]) -> None:
    _, lock_file = frozen_files
    lock_file.write_text(
        lock_file.read_text(encoding="utf-8").replace(
            _compiled_record("homeassistant", "2026.11.0"),
            f"homeassistant==2026.11.0 {chr(92)}\n",
        ),
        encoding="utf-8",
    )
    with pytest.raises(MinimumPolicyError, match="SHA-256 hash"):
        verify_frozen_minimum_inputs()
