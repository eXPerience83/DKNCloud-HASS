# Issue #197 — cumulative final audit handoff (2026-11-18)

> **Status: independent final Astra audit is REQUESTED, NOT COMPLETED by this document.**
> This is a bounded review brief, not a release certificate or physical-device
> acceptance report. Follow [issue #197](https://github.com/eXPerience83/DKNCloud-HASS/issues/197)
> for the chronological evidence and [#121](https://github.com/eXPerience83/DKNCloud-HASS/issues/121) /
> [#191](https://github.com/eXPerience83/DKNCloud-HASS/issues/191) for release and
> real-installation gates.

## Baseline and already integrated work

- Audited source baseline for this handoff: `main` commit
  `8891fa9f28c2a928ad6e891e3a52caf1158fd8b1` (17–18 Nov 2026).
  **Re-read the actual HEAD before reviewing or approving a new PR.**
- `v0.6.0a1` and `v0.6.0b1` are published experimental prereleases;
  `v0.6.0b1` was published on 2026-11-17. Neither is stable-certified.
- Already completed: #197 phases 1–4 and focused stage-5/7 work in
  PRs #198–#214, follow-up race/ambiguous-write remediation in
  #215–#222, the #228 synchronization diagnostic and #229 beta prep.
  Do not reimplement #210's platform/helper work, the protected offline LAB,
  or #198/#199's CI performance changes.
- **Not in this cleanup PR:** #223 is a separate open draft LAB experiment.
  Its five read-isolation trials support INFO-associated Pusher delivery for
  one numeric limit field, not general HVAC commands or causal proof.

## Exact current CI evidence (main at 8891fa9f)

| Signal | Current HA | Hash-locked HA minimum |
| --- | --- | --- |
| Workflow | [tests run 44198267709](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/44198267709) | [minimum run 44198267820](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/44198267820) |
| Environment | HA + official pytest 2026.11.1 | HA + official pytest 2026.11.0 |
| Results | 937 passed, 17 warnings | 937 passed, 17 warnings |
| Statement coverage | 90.07% | 90.07% |

Both independent workflows passed on the quoted source SHA; the separate
isolated HA loopback smoke and fail-closed aggregate `tests` status passed.
Quality, HACS/hassfest, package and workflow-security checks also passed on
that SHA. This does **not** assert that later PR heads have passed.

### Coverage review targets — prioritize risk, not percentages

Statement coverage is not branch coverage or real-hardware validation.
Inspect uncovered branches and assert behavior at callsites before adding tests:

| Runtime module | Statements not covered | Statement coverage | Focus |
| --- | ---: | ---: | --- |
| `sensor.py` | 28 | 81.94% | Defensive state formatting; new 0.6.0b1 sync-mode lifecycle / auth transitions |
| `helpers.py` | 47 | 83.45% | Power intent, optimistic overlay, Sleep/wake, unload/recovery boundaries |
| `__init__.py` | 93 | 87.01% | setup/unload, reauthentication, task ownership and cancellation |
| `airzone_api.py` | 35 | 87.13% | bounded retries, 401/429/5xx, ambiguous writes without replay |
| `sync_manager.py` | 38 | 89.76% | subscription races, stale GET precedence, OFF corroboration |

Current required thresholds are lower (74% overall; focused per-module floors).
Do not arbitrarily raise gates, delete test assertions, shorten protected LAB
deadlines or claim that a high statement percentage proves concurrency safety.

## Proven residual maintenance on this handoff branch

1. The obsolete `sleep-stack.yml` workflow targeted only the old
   `fix/171-*` / `feat/171-*` branches, with an unreachable special full-suite
   branch condition. No matching branch was present on 2026-11-18, #171
   is closed and the **live active main ruleset** requires eight other
   checks, not Sleep stacked validation. The full Sleep regressions remain
   covered by normal required CI. Remove only this retired workflow.
2. `CONTRIBUTING.md` had mislabeled HA 2026.11.0 as the *primary* pinned
   baseline; current lock uses 2026.11.1, while the hash-locked minimum is
   2026.11.0. `scripts/check_coverage.py` retained a legacy PHACC docstring.
3. User README and smoke checklist still described `v0.6.0b1` as merely
   proposed despite the published release. Update publication language
   without declaring hardware / Marketplace acceptance.

## Explicitly deferred, not silently forgotten

- HA baseline updater still exits successfully when any historical PR uses
  its predetermined head branch; a stale open PR may therefore remain based
  on outdated main. See #197 discussion and #195. **Separate scoped change**
  only after examining ownership, full changed-file scope, current base SHA,
  race handling and fresh required CI. Never auto-rewrite a human branch.
- Do **not** change default-on automatic Sleep cleanup merely because
  `info.md` originally framed default-off as a recommendation. Runtime,
  README and tests reflect the chosen default; any policy change requires
  a separate product decision, migration/release notes and device acceptance.
- No real HVAC writes or new authenticated cloud experiments. The physical
  Sleep, reauthentication, WServer, Marketplace/HACS install and migration
  acceptance work remains tracked in #191/#121/#105/#101.
- Preserve the known DKN API contract and WSS-only service-scoped TLS
  policy (#60), existing domain `airzoneclouddaikin`, unique IDs, options
  and default user behavior. No release/version bump is authorized here.

## Independent GPT-6.2 Astra review assignment

Use the **final PR HEAD and its base**, not this historical checkpoint.
Perform a fresh whole-repository audit (code, tests, docs, packaging and
workflow policy) and use the #197 PR history as evidence, **not a proof of
correctness**. Focus on:

1. Cross-task races in P1/P2/P3/P4/P7/P8 commands, scene/Sleep preparation,
   intent supersession and post-unload publication. Reproduction with
   deterministic barriers is preferred over hypothetical findings.
2. Startup and teardown ownership; Pusher ACK/disconnect/resync, recovery
   pool and budget fairness; stale GET/Pusher precedence; no duplicate
   Sleep OFF corroboration.
3. Network/security, error semantics, 401/429/5xx, non-replay of uncertain
   controls, private diagnostics, WSS-only TLS, secrets and offline LAB safety.
4. New beta synchronization-mode sensor: entity lifecycle, updates on
   auth/transport changes, state translations and no extra network requests.
5. Missing high-risk coverage, false-positive synthetic assertions,
   unreachable code and stale docs; explain *why* a proposed deletion or
   deduplication is equivalent. Do not repeat completed helper refactors.
6. Exact-head required CI: current and independently pinned minimum HA
   suites, fail-closed `tests` + isolated smoke, coverage, Ruff, HACS,
   hassfest, ZIP, actionlint/zizmor and CodeQL. Record CodeRabbit review
   as approved, skipped or rate-limited exactly as observed.

Deliver a **GO / NO-GO / CONDITIONAL** verdict with evidence, severity,
file/line references, reproduction or a concrete call chain, regression
recommendation and explicit unverified items. If blockers exist, propose
focused follow-up PRs; otherwise conclude only that *synthetic/repository*
gates pass, **not** universal hardware or release-readiness. Do not merge,
publish, alter main protection or operate equipment as part of this review.
