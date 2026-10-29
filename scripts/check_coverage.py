"""Read pytest-cov's JSON output and enforce evidence-based coverage floors.

Only the current locked HA job runs pytest-cov. Minimum-HA tests stay isolated
and still exercise the entire suite without installing additional tooling.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def report_coverage(
    report_path: Path,
    total_floor: float,
    module_floors: dict[str, float],
) -> tuple[str, list[str]]:
    """Render per-module report and return explicit threshold violations."""
    document = json.loads(report_path.read_text(encoding="utf-8"))
    totals = document.get("totals")
    files = document.get("files")
    if not isinstance(totals, dict) or not isinstance(files, dict) or not files:
        raise ValueError("coverage JSON is missing totals or module records")

    total_percent = float(totals["percent_covered"])
    rows = [
        "## DKN Cloud statement coverage",
        "",
        f"Overall: **{total_percent:.2f}%** (required: {total_floor:.2f}%)",
        "",
        "| Integration module | Statements | Missing | Covered |",
        "| --- | ---: | ---: | ---: |",
    ]
    seen: dict[str, float] = {}
    errors: list[str] = []
    for path, details in sorted(files.items()):
        if not path.startswith("custom_components/airzoneclouddaikin/"):
            continue
        filename = Path(path).name
        if filename in seen:
            raise ValueError(f"duplicate module basename: {filename}")
        summary = details["summary"]
        percent = float(summary["percent_covered"])
        seen[filename] = percent
        rows.append(
            f"| `{filename}` | {summary['num_statements']} | "
            f"{summary['missing_lines']} | {percent:.2f}% |"
        )
    if not seen:
        raise ValueError("no DKN runtime modules in coverage report")
    if total_percent + 1e-8 < total_floor:
        errors.append(f"Overall {total_percent:.2f}% below {total_floor:.2f}%")
    for module, floor in module_floors.items():
        if module not in seen:
            errors.append(f"Expected module not measured: {module}")
        elif seen[module] + 1e-8 < floor:
            errors.append(f"{module}: {seen[module]:.2f}% below {floor:.2f}%")
    rows.extend(
        ["", "**Result:** " + ("FAILED" if errors else "thresholds met")]
    )
    if errors:
        rows.extend(["", *[f"- {message}" for message in errors]])
    return "\n".join(rows) + "\n", errors


def main() -> int:
    """Validate complete report; never round up to bypass a threshold."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-total", type=float, required=True)
    parser.add_argument(
        "--module-min", action="append", default=[], metavar="MODULE=PERCENT"
    )
    args = parser.parse_args()
    thresholds: dict[str, float] = {}
    for item in args.module_min:
        module, equals, raw = item.partition("=")
        if not equals or not module or module in thresholds:
            parser.error(f"expected unique module=percent: {item!r}")
        thresholds[module] = float(raw)

    for name, floor in [("overall", args.min_total), *thresholds.items()]:
        if not 0.0 <= floor <= 100.0:
            parser.error(f"{name} floor must be between 0 and 100")

    try:
        text, violations = report_coverage(
            args.report, args.min_total, thresholds
        )
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Invalid coverage report: {error}", file=sys.stderr)
        return 2

    print(text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with Path(summary_file).open("a", encoding="utf-8") as stream:
            stream.write(text)
    if violations:
        for violation in violations:
            print(f"Coverage gate: {violation}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
