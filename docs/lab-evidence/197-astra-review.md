# Independent Astra review: LAB gates and 0.6.0 decision boundary

Review performed by a separate `gpt-6-astra` agent against the uncommitted
follow-up to PR #223 at `aa4d0f5`, including the transport, fixed experiment
profiles, exact approvals, snapshot provenance and synthetic wall-clock flag.
The reviewer did not edit files, access credentials or connect to real DKN Cloud.

## Code findings

No blocking defect was found in the reviewed changes. Persistent INFO/GET gates
cover existing budget waiters, drain delegated reads and enforce one GET permit.
Exact experiment approval, OFF/Home requirements, two PUT attempts,
ambiguous-outcome revocation and unchanged session deadlines remain enforced.
Production integration code is unchanged. The reviewer independently ran the
three focused barrier, recovery-evidence and grant suites: **20 passed**.

Raw GET receipt and actual field application are separate. A newly completed matching raw GET field plus matching raw SyncManager state
corroborates restoration; optimistic state alone cannot authorize it. An identical
Pusher value may legitimately be retained by revision fencing instead of applying
the GET field. The reviewer separately approved this localized correction after
the stronger final regression exposed that distinction. Ambiguity revokes restoration even for synchronous
failure before transport context creation. `--fixture-wall-clock` is rejected in
real modes before grant/credential access and cannot select a real backend.

At the review's completion the 80 s wall-clock case was still running. Crossing
60 s quiet age does not prove the periodic watchdog callback fired: it samples
periodically. Do not claim specifically observed watchdog-generated waiter
coverage without observing/asserting that callback. Generic queued-read
regressions independently cover admission, cancellation and drain behavior.

## Evidence and required experiments

The sole existing real minimum-limit trial is consistent with INFO-associated
delivery, not proven causality, periodicity, physical convergence or scene/Sleep
behavior. External-client quietness was unconfirmed. Its full isolation check
correctly failed because an INFO escaped before PUT after the nominal marker.
Later telemetry corroborated restoration. New gate code must not retroactively
turn that original result into a pass.

The required order is INFO-first, GET-first, spontaneous, GET-first repeat,
INFO-first repeat. All five new real trials await separate fresh session and
numeric approvals; see the [comparison table](197-info-pusher-get.md). Each must
independently corroborate its baseline and restoration. Unknown writes stop the
series without automatic replay or restoration. Absence within a finite window
is a censored observation. Other clients and an unknown periodic backend refresh
remain alternative explanations for arrival after either INFO or GET.

The 175/215/210 s pre-PUT reserves fit normal measured responses but do not
guarantee restoration after arbitrary delays. The 285 s active / 300 s parent
bounds remain authoritative; slow/failing requests may require owner intervention.
Use `get_first_push`, `info_phase_push` and admission timestamps to separate
GET-first phases. Legacy `put_push_after_resume` aggregates both post-isolation
phases and cannot by itself establish after-INFO delivery.

## Proposed minimum policy for 0.6.0

- **Successful PUT:** retain ordinary INFO/Pusher synchronization and the bounded
  optimistic overlay. HTTP acceptance alone is not telemetry confirmation.
  Do not add an unconditional GET or change cadence based on this real trace.
- **Timeout/unknown PUT:** propagate the unknown result, never replay, request
  coalesced recovery and preserve known-unsent cooldown handling. Independently
  review generic Home/Away scene ambiguity for parity with number PUTs; this
  symmetry gap is distinct from successful minimum-limit delivery evidence.
- **GET timing:** retain current ordering pending the five real observations.
  Consistently useful GET-first snapshots would support reviewing a scoped,
  coalesced read. Stale snapshots and outside clients remain limitations.
- **Optimistic duration:** retain the current 30 s bound; partial/unrelated
  events must not acknowledge a field. Expiry requests bounded/coalesced recovery.
- **Fallback without Pusher:** preserve existing INFO/GET spacing, backoff,
  cooldown, authentication and shutdown behavior. An INFO failure is not proof
  that a later GET corroborates a preceding write.
- **Sleep:** preserve session revisions, explicit rearm, cleanup ambiguity fences
  and fresh scene/power requirements. Do not extrapolate minimum-limit findings
  to Sleep, scenes or P-channel controls.

No production policy change is approved by this review. The five new real trials
are still pending, so a final measured 0.6.0 tuning decision remains deferred.

## Regression and acceptance criteria

Preserve pre-/post-ACK matching push, partial-event nonconfirmation, stale and
concurrent GET fencing, optimistic expiry and coalesced recovery. Unknown
outcomes (timeout/5xx/cancellation) consume one write attempt without replay or
automatic restoration. Fallback spacing, cooldown and teardown must remain intact.
Every accepted real run needs exact phase admission transcripts, unchanged
OFF/Home, at most two PUT attempts, matching restoration telemetry or explicit
owner intervention, and closed resources. Keep isolation violations visible;
never count restoration events as equivalent independent experiments by default.


## Implementer's post-review validation addendum

The standalone full-duration synthetic case subsequently passed: 80.009 s after
ACK without LAB INFO/GET, no GET during the INFO-only phase, exactly two controls,
restoration corroborated and resources closed. The stale GET-first HA case passed
on both pinned versions after Astra approved the raw-GET/raw-manager criterion.
These are implementer-observed outcomes, separate from Astra's independent
20-test rerun. The full-duration trace is linked in the experiment report.
