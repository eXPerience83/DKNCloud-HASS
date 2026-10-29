"""Exercise the coverage report gate without Home Assistant/network dependencies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_coverage import report_coverage


def _json_report(tmp_path: Path, *, overall: float = 74.25) -> Path:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {"percent_covered": overall},
                "files": {
                    "custom_components/airzoneclouddaikin/__init__.py": {
                        "summary": {
                            "percent_covered": 71.5,
                            "num_statements": 500,
                            "missing_lines": 140,
                        },
                    },
                    "custom_components/airzoneclouddaikin/climate.py": {
                        "summary": {
                            "percent_covered": 51.0,
                            "num_statements": 400,
                            "missing_lines": 196,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return report


def test_report_contains_all_runtime_modules_and_global_gate(tmp_path: Path) -> None:
    """Success reports both global and individually tracked statement coverage."""
    text, violations = report_coverage(
        _json_report(tmp_path),
        70.0,
        {"__init__.py": 70.0, "climate.py": 50.0},
    )
    assert violations == []
    assert "74.25%" in text
    assert "`climate.py`" in text
    assert "thresholds met" in text


def test_total_and_module_regression_fail_independently(tmp_path: Path) -> None:
    """A low global percentage cannot be concealed by good module coverage."""
    _, violations = report_coverage(
        _json_report(tmp_path, overall=65.0),
        70.0,
        {"climate.py": 55.0},
    )
    assert len(violations) == 2
    assert "Overall" in violations[0]
    assert "climate.py" in violations[1]


def test_missing_runtime_module_fails_closed(tmp_path: Path) -> None:
    """A renamed/deleted integration module never bypasses its floor."""
    _, violations = report_coverage(
        _json_report(tmp_path), 70.0, {"airzone_api.py": 75.0}
    )
    assert violations == ["Expected module not measured: airzone_api.py"]


def test_malformed_report_fails_closed(tmp_path: Path) -> None:
    """Do not accept incomplete or unrelated JSON as a coverage report."""
    report = tmp_path / "coverage.json"
    report.write_text('{"totals": {}, "files": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing totals"):
        report_coverage(report, 70.0, {})
