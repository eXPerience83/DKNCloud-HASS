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

## Follow-up scheduling experiment

Change only the xdist distribution algorithm to `--dist=load` in the
two existing full-suite jobs. Keep `-n 2` because GitHub-hosted standard
runners offer bounded CPU resources. This lets workers receive separate
parametrized protected-lab cases while retaining worker process isolation.
It does **not** shorten deadlines, bypass protected-lab scenarios, mock
backend barriers, or skip coverage collection. The independent minimum HA
hash-locked graph and the current locked HA graph continue to run separately.

Acceptance criteria: all required checks, both 750-test suites and the
existing statement-coverage gates must remain green; compare total workflow
and pytest times against the above before/after runs, including possible
variance and test fixture isolation. If worker distribution reveals flaky
tests or slows the critical path, revert to `loadfile` and investigate
the specific test isolation before trying again.

The normal workflow also performs an independent, fully synthetic HA loopback
smoke after pytest. It takes roughly 2 minutes and is a potential separate
optimization. Do **not** detach it without preserving the protected
`tests` status check as a failure-sensitive aggregate for both the suite
and smoke test. Do not relax the repository ruleset to make such a change.
