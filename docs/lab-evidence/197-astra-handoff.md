# Astra handoff — resume after the five real trials

Status: experiments finished; **final independent review deferred** at the owner's
request. Do not start new trials, production changes, merges or releases from
this document. Earlier preliminary Astra notes are historical, not a final
review of all five outcomes.

## Preserved outcome

The source tested was `58c7a7dcf07d4f232ccbe22f70df1764f92aba91` in PR #223.
Five separately approved sessions changed only `min_temp_unoccupied` 16→17→16 °C
on the selected OFF/Home device. Exactly ten PUTs returned HTTP 204. All five
restorations were corroborated by Pusher and final raw GET, with OFF/Home/mode
checks passing. All processes/sockets/tasks closed; no HTTP tasks or disposable
HA configuration remained. Durable private grants/journals remain consumed.

| Trial | Order | Silent post-ACK window | GET17 without post-PUT INFO | First matching Pusher lag after first resumed INFO |
| --- | --- | ---: | --- | ---: |
| 1 | INFO first | 45.003 s | Not probed | 10.678 s, after two INFOs |
| 2 | GET first | 45.006 s | Yes; no Pusher for next 20 s | 2.450 s |
| 3 | Spontaneous | 80.011 s | Not probed | 16.601 s, after two INFOs |
| 4 | GET first repeat | 45.005 s | Yes; no Pusher for next 20 s | 3.234 s |
| 5 | INFO first repeat | 45.006 s | Not probed | 2.080 s |

No matching Pusher arrived in any silent window. All first matching Pusher events
followed INFO; in trials 1/3/5 they preceded any post-PUT GET. Both GET-first
repetitions demonstrate updated **cloud snapshot** visibility without INFO.
Association is not causality. An unknown backend timer, delayed work and outside
traffic remain limitations; other-client quietness was owner-reported only.
These successful minimum-field/OFF/Home trials establish neither scene/Sleep
semantics, physical convergence nor real ambiguous-write recovery.

## Files to read, without repeating discovery

- [Full report and constraints](197-info-pusher-get.md).
- [Summary JSON](197-series-summary.json), [CSV](197-series-summary.csv).
- Complete raw allowlisted timelines: [1](197-trial-1-info-first.json),
  [2](197-trial-2-get-first.json), [3](197-trial-3-spontaneous.json),
  [4](197-trial-4-get-first.json), [5](197-trial-5-info-first.json).
- [Historical failed-isolation trace](197-minimum-put-baseline.json),
  [80 s synthetic trace](197-synthetic-80s.json),
  [earlier preliminary Astra review](197-astra-review.md).
- Runtime: `scripts/protected_lab.py`, `put_trial.py`, `read_experiment.py`,
  `put_scenario.py`, `session_grant.py`, `run_supervised_ha.py`,
  `protected_ha_lab/__init__.py`, `trial_timeline.py`.
- Regressions: `tests/test_read_barriers.py`, `test_put_recovery_evidence.py`,
  `test_supervised_session_runner.py`, `test_session_grant.py`.
- Existing production behavior: `sync_manager.py`, `sync_state.py`,
  `optimistic.py`, `request_budget.py`, `number.py`, `climate.py`, and `info.md`
  under the integration paths as applicable. Follow their current names/imports.
- PR #222 was independently read at head
  `6ca8e14115a2e6a92c4f422da489e068e6c1753b`: ambiguity readback and unload/Sleep
  guards are already proposed there; do not duplicate them in #223.

## Review questions and pitfalls

1. Successful PUT: does this evidence justify any change beyond ordinary INFO /
   bounded optimism, or merely preserving the current policy?
2. Ambiguous PUT: how should PR #222 use coalesced GET when a matching readback
   proves only cloud-reported value and a stale mismatch does not prove nonreceipt?
   Neither case authorizes replay. No real timeout was induced.
3. Do not hardcode 45 s as the earliest useful GET: that is when the experiment
   deliberately allowed it, not a measured availability threshold.
4. Trial 3 has `info_phase_push=false` because its phase ended at GET-gate opening.
   Actual Pusher preceded the first GET send. Analyze transport times, not that
   coarse flag or HA optimism. Revision fencing can retain identical Pusher state
   rather than apply a matching raw GET; corroboration is not GET application.
5. Optimistic TTL, INFO cadence, fallback without Pusher, Sleep exceptions and
   protections need a reasoned 0.6.0 policy, minimal proposed changes, regressions
   and acceptance criteria. The artificial read suspensions do not themselves
   justify increasing the current optimistic duration.
6. Session origins reset and gaps were not monitored. Do not infer a backend
   refresh period from this record or from request spacing imposed by the LAB.

All seven source-commit workflows passed, including **996 tests on each pinned
HA version** and coverage gates. The five synthetic ambiguity/fallback cases and
long isolation are recorded in the report. No new general tests/audit were run
after the owner stopped further investigation. The final evidence commit is
only documentation/data; runtime remains the tested source. No production policy
was modified or approved for deployment. Resume with these files, not old
credentials or consumed authorizations; any further real session needs new scope
and exact numeric approval.
