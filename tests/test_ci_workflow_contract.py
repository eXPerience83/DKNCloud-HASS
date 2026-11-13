"""Protect the required CI check when pytest and smoke run in parallel."""

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
)


def test_full_suite_and_independent_smoke_both_gate_required_tests() -> None:
    """Never let a successful parallel branch mask a failing/skipped sibling."""
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = text.split("\njobs:\n", 1)[1]
    pytest_job = jobs.split("\n  pytest:\n", 1)[1].split("\n  smoke:\n", 1)[0]
    smoke_job = jobs.split("\n  smoke:\n", 1)[1].split("\n  tests:\n", 1)[0]
    aggregate = jobs.split("\n  tests:\n", 1)[1]

    assert "-n 2 --dist=loadfile" in pytest_job
    assert "python -m pytest" in pytest_job
    assert "--cov-report=json:coverage.json" in pytest_job
    assert "scripts/check_coverage.py" in pytest_job
    assert "Run independent Home Assistant loopback smoke" not in pytest_job
    assert "scripts/run_isolated_ha.py" in smoke_job
    assert "TMPDIR: ${{ runner.temp }}" in smoke_job

    for job in (pytest_job, smoke_job):
        assert "uv lock --check" in job
        assert "uv sync --locked --only-group test" in job
        assert "uv pip check --python .venv/bin/python" in job

    assert "if: ${{ always() }}" in aggregate
    assert "needs:\n      - pytest\n      - smoke" in aggregate
    assert "PYTEST_RESULT: ${{ needs.pytest.result }}" in aggregate
    assert "SMOKE_RESULT: ${{ needs.smoke.result }}" in aggregate
    assert '"$PYTEST_RESULT" != "success"' in aggregate
    assert '"$SMOKE_RESULT" != "success"' in aggregate
    assert "exit 1" in aggregate
