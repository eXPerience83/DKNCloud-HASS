"""Protect both independent HA lanes from silent infinite pytest hangs."""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def test_official_pytest_faulthandler_fails_stalled_tests(pytestconfig):
    """The locked harness must support the built-in pytest 9 hard timeout."""
    assert float(pytestconfig.getini("faulthandler_timeout")) == 180.0
    assert pytestconfig.getini("faulthandler_exit_on_timeout") is True


@pytest.mark.parametrize(
    ("path", "job"),
    [
        (".github/workflows/tests.yml", "pytest"),
        (".github/workflows/ha-minimum-compatibility.yml", "minimum-compatibility"),
    ],
)
def test_required_ha_jobs_preserve_full_suites_and_bound_crashed_workers(path, job):
    """A timed-out xdist worker must fail rather than restart indefinitely."""
    workflow = (_ROOT / path).read_text(encoding="utf-8")
    assert f"  {job}:\n    runs-on: ubuntu-latest\n    timeout-minutes: 20" in workflow
    assert "-n 2 --dist=loadfile" in workflow
    assert "--max-worker-restart=0" in workflow
    assert "--cov=custom_components.airzoneclouddaikin" in workflow
    assert "scripts/check_coverage.py" in workflow
