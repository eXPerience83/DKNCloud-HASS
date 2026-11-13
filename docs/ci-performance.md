# CI performance baseline and profiling

Maintainer notes for [PR #198](https://github.com/eXPerience83/DKNCloud-HASS/pull/198), measured on 2026-11-13/14 UTC.
This is an Actions/test-infrastructure change only. It does not modify the
integration, HA support floor, dependency hashes, pytest coverage thresholds, or
the repository's required status-check names.

## Reproducible comparison

Both rows of the following comparisons use the same Home Assistant test policy
and the same 750 test cases; this table records *actual* GitHub Actions runs,
not a projected speedup. The before runs are from `main` immediately before
the first optimization; the first optimized runs used two pytest-xdist workers
with `--dist=loadfile`.

| Check | Before (main) | Two workers, grouped by file | Reduction |
| --- | ---: | ---: | ---: |
| Current locked HA pytest, test command | 552.13 s | 455.58 s | 17.49% |
| Independent minimum HA pytest, test command | 545.59 s | 468.20 s | 14.18% |
| Entire `tests` workflow | 12m 06s | 10m 16s | 15.15% |
| Entire `minimum-compatibility` workflow | 9m 33s | 8m 29s | 11.17% |

Evidence:

- Main locked workflow: [run 43520449391](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43520449391), commit `3a191dac34cbb27a4a9770dfe7ad94d201553af9`.
- Main minimum floor: [run 43520449430](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43520449430), same commit.
- First optimized locked workflow: [run 43523825195](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43523825195), commit `df24070ffda351f323c8d49d7370b05acaab5684`.
- First optimized minimum floor: [run 43523825155](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43523825155), same commit.

All four runs passed 750 tests. Both optimized suites reported 88.04%
statement coverage (74% required overall, plus unchanged per-module floors).
Observed warning counts changed from 7 to 17 when pytest was distributed
across workers; review warnings in the logs, rather than treating the count
alone as a failure or hiding them. Caches and runner scheduling can vary
between runs; these numbers are single-run measurements, not statistical
averages.

## Where the time goes

The `--durations=20` profile on the first optimized locked run identifies
long-running, fully synthetic protected-laboratory subprocess tests:

| Example test case | Call time |
| --- | ---: |
| `test_protected_lab.py::test_independent_protected_launcher[reconnect_no_events]` | 120.84 s |
| `test_independent_protected_launcher[invalid_event]` | 60.86 s |
| `test_independent_protected_launcher[no_events]` | 60.86 s |
| `test_independent_protected_launcher[wrong_device]` | 60.81 s |
| `test_independent_protected_launcher[reconnect]` | 51.00 s |
| `test_readonly_runtime.py::test_real_entry_loopback_and_control_barrier` | 50.12 s |

With `--dist=loadfile`, pytest-xdist keeps all tests in the protected-lab
file on a single worker; its serial subprocess cases dominate wall time while
the other worker can finish earlier. Installation/uv-cache work is not the
primary bottleneck.

## `--dist=load` experiment (reverted)

Both test lanes passed with two workers using `--dist=load` (same cases and
coverage floors), but it did **not** improve time over `loadfile`:

| Check | `--dist=loadfile` | `--dist=load` | Outcome |
| --- | ---: | ---: | --- |
| Current locked HA pytest | 455.58 s | 470.81 s | 15.23 s slower |
| Independent minimum HA pytest | 468.20 s | 478.85 s | 10.65 s slower |
| Entire `tests` workflow | 10m 16s | 10m 43s | 27 s slower |
| Entire minimum HA workflow | 8m 29s | 8m 27s | Similar, within variation |

Evidence: [locked `load` run](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43525591536)
and [minimum `load` run](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43525591634)
(commit `adf4e4fca8c24dd275878b2f4377a6fc2170abca`).
Both completed 750 tests; locked coverage 88.04%, minimum HA coverage
88.16%. The slightly different minimum coverage is a measurement variation
from worker/test ordering, not a threshold change. These are single-run
measurements. The `load` strategy is now reverted to the previously green,
faster `--dist=loadfile` in both jobs.

## Isolated HA smoke: parallel required check

The original `tests` job executed `scripts/run_isolated_ha.py` **after**
the approximately 7m35s pytest/coverage suite, extending workflow completion
by about two minutes even though the smoke is an independent synthetic HA
process. The second optimization preserves the safety contract while moving
the work in parallel:

- **`pytest` job:** same locked HA test graph, full suite, coverage JSON/XML
  artifacts, coverage thresholds and independent release-dependency check.
- **`smoke` job:** separate GitHub-hosted runner, same Python version and
  `uv.lock` test environment, fresh isolated `TMPDIR`, the unchanged
  `scripts/run_isolated_ha.py` smoke command, and no production credentials.
- **`tests` job (required by main ruleset and release policy):** a
  fail-closed aggregation job with `needs: [pytest, smoke]` and
  `if: always()`. It must fail if either prerequisite has any result
  other than `success`, including a skipped or cancelled prerequisite.
  Both jobs must succeed before the existing required `tests` check passes.

This layout does not shorten the protected-lab deadlines, bypass any checks,
weaken coverage, change the hash-locked minimum HA lane, or modify production
integration code. It retains all existing required check names and their
GitHub Actions identities; branch protection does not need to be edited.

**Validation gate:** both upstream jobs and aggregate `tests`, full minimum
HA suite, actionlint, zizmor, Ruff, HACS, hassfest, package and CodeQL must
pass on the new PR head. Inspect actual per-job times; the ~2-minute benefit
is an estimate until GitHub Actions confirms it. The separate smoke runner
adds environment setup overhead, so record both latency and extra runner
minutes. Do not claim that a green status implies hardware acceptance.

Further safe work: investigate deliberate 50–121-second synthetic lab waits
against their scenario contracts and review runtime/fixtures only when
evidence supports a specific optimization.
