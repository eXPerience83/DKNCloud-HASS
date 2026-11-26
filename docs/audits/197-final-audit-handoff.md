# Issue #197 — cumulative final audit handoff (2026-11-26)

> Independent final Astra audit **pending**. This checkpoint is documentation,
> not approval of a release, device acceptance, or live experiments.
> [#197](https://github.com/eXPerience83/DKNCloud-HASS/issues/197) is the audit record;
> [#253](https://github.com/eXPerience83/DKNCloud-HASS/issues/253) is the stabilization roadmap.
> The historical 18 November brief is preserved below.

## Current audit baseline and gates

- Published experimental prerelease: **v0.6.0b2**, 24 November 2026.
  Its installed HA 2026.11.3 [diagnostic snapshot](https://github.com/eXPerience83/DKNCloud-HASS/issues/191#issuecomment-7145987037)
  shows healthy PUSH, not verified physical freshness.
- Compare original v0.6.0a1 source `6ad2c77c7ae48f14d24b9d9fb0cc8b5690a2495d`
  with the actual final main HEAD after this documentation PR.
  Current verified runtime baseline: `01b6419026e6ce14c77df699c365a90c6b418bc4`, tree
  `1c6860d1548e029cebc06b93c1e5ae9f473fdfb9`.
- #231 (ambiguous PUT replay), #232 (runtime ownership), #233 (fresh GET),
  #238 (latest power intent; supersedes #234), #244 (simplified Sleep),
  #246/#248 (Away and Climate/Switch parity), #247 (documentation),
  #251 (H03/H04), #252 (H05), and #254 (explicit scene intent #249)
  are integrated. H06/H07 and #58 remain deliberately deferred;
  H09 CI safeguards retained. #252 metadata was reconciled without repeating
  the merge; #254 merged normally.
- #49's historical first-disagreeing-refresh rule is superseded by the
  bounded overlay policy (matching explicitly received fields, TTL, recovery).
  [Disposition](https://github.com/eXPerience83/DKNCloud-HASS/issues/49#issuecomment-7184854410).
  Do not add indiscriminate invalidation or claim physical convergence.

### Exact main CI at 01b6419

| Gate | Evidence |
| --- | --- |
| Current HA suite | [1076 passed, 17 warnings](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/45771026730/job/139743425837) |
| Hash-locked minimum HA suite | [1076 passed, 17 warnings](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/45771026808/job/139743425784) |
| Statement coverage | 3784 statements, 327 missed, 91% rounded in each lane |
| Isolated smoke and aggregate tests | [passed](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/45771026730) |
| CodeQL Python and Actions | [passed](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/45771027024) |
| Other checks | Ruff, package, HACS/hassfest, actionlint/zizmor and validation passed |

All 13 check runs succeeded on this SHA, including a separate validation run.
The earlier scheduled CodeQL SARIF-upload Server Error at `4a6afe7` remains
historical; the new main passed normal analysis without changing protections.
#254 reviewed HEAD `cec4713ba41577a52e4b65ce00b252eb0e768525` had 14/14
checks including full CodeRabbit, with its one valid timeout-classification
finding corrected and closed. Its squash tree is identical to current main.
Recheck exact HEAD after any further merge; do not transfer CI status to a
different commit. Local independent final audit remains pending.

## Archived LAB evidence — do not merge the experimental implementations

- #223: branch `lab/197-bounded-setpoint-implementation`, final HEAD
  `8aeeb229eb41e65d3a391f3cfd3d4f909e9faa96`; evidence ancestor
  `859bd094c1c6918690311a46c187a06a8b404b2f`.
  [Five-trial report](https://github.com/eXPerience83/DKNCloud-HASS/blob/859bd094c1c6918690311a46c187a06a8b404b2f/docs/lab-evidence/197-info-pusher-get.md),
  [JSON summary](https://github.com/eXPerience83/DKNCloud-HASS/blob/859bd094c1c6918690311a46c187a06a8b404b2f/docs/lab-evidence/197-series-summary.json),
  [subsequent read-only policy assessment](https://github.com/eXPerience83/DKNCloud-HASS/blob/8aeeb229eb41e65d3a391f3cfd3d4f909e9faa96/docs/lab-evidence/197-sync-policy-assessment.md).
  Five approved numeric-field trials, ten HTTP 204 PUTs, five corroborated
  restorations. Matching PUSH followed INFO resumption in each trial; this is
  association, not causality, a universal timer, or physical-state proof.
  The separate earlier gate-violation trial remains explicitly limited.
- #236 / #237: branch `lab/236-sleep-five-minute-probes`, HEAD
  `2e91d01aee5601321aab420eff3bdbe4cfe280f7`.
  [A/C/revised B report](https://github.com/eXPerience83/DKNCloud-HASS/blob/2e91d01aee5601321aab420eff3bdbe4cfe280f7/docs/lab-evidence/236-sleep-live-results.md),
  [consolidated observations](https://github.com/eXPerience83/DKNCloud-HASS/issues/236#issuecomment-7069634138),
  [sanitized recorder cross-check](https://github.com/eXPerience83/DKNCloud-HASS/issues/236#issuecomment-7069667905).
  Observations support countdown from Sleep selection, retained Sleep after
  early OFF, and shutdown near the original deadline after re-wake before
  expiry. F was not performed or authorized. #245 (whether Home cancels
  the native timer after manual OFF) remains a separate nonblocking hypothesis.
  Historical proposed policy is not the final #244 implementation.
- Preserve both branches and their PR history; do not reuse expired grants,
  publish raw diagnostics, merge experimental runners, or conduct new traffic.
  The production policy is `info.md`, not the historical LAB proposals.

## Independent audit assignment and remaining gates

1. Reproduce and review #231/#232/#233/#238, ownership/dispatch/readback,
   Sleep/Away/Home and latest explicit intent (#254); consider interactions
   across platforms, fresh GET/PUSH precedence and overlays.
2. Verify H01–H05/H08/H09, no unnecessary H06/H07 refactoring. Check security,
   auth, 401/429/5xx, known-unsent connection versus ambiguous response
   timeout, privacy and the WSS-only TLS exception (#60).
3. Run exact locked Python 3.14/current HA and independently pinned minimum HA,
   isolated smoke, lint and release/package/security checks. Prefer targeted
   deterministic reproductions or reversible mutations to speculative defects.
   Report SHA, evidence, severity and explicit unverified items.
4. #243 remains optional display-only persistence: unknown t0 does not prevent
   cleanup on valid startup OFF/Sleep under #244. #105 real password-change,
   #101 physical external convergence and #191 final ZIP, install/migration,
   lifecycle and hardware gates remain open. Reassess #60's lack of forward
   secrecy before stable. #109/#173 coordinated CI maintenance, #58/H06/H07
   structural work deferred. No real traffic authorized.
5. After #197 independent verdict and exact-main gates, prepare b3 separately
   (version/changelog/package PR, manual release approval); RC1 and stable
   depend on acceptance, not just passing synthetic suites.

Do not merge, publish, change protection rules, or operate equipment as part
of the independent audit itself.

---

<details>
<summary>Historical 18 November handoff — retained for traceability, superseded above</summary>

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

</details>
