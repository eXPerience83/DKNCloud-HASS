# INFO / Pusher / GET experiments (#197 / #223)

## Real evidence already collected

[Original issue report](https://github.com/eXPerience83/DKNCloud-HASS/issues/197#issuecomment-6897005472)
and [anonymous monotonic trace](197-minimum-put-baseline.json) describe the
previous **real** 16 → 17 → 16 °C minimum-limit trial. It is not one of the five
new trials. Both PUTs returned HTTP 204. The device remained OFF/Home; restoration
was corroborated by Pusher and final GET. No account or device IDs are published.

Relative to first PUT transport admission: ACK +0.067 s, resumed INFO +20.069 s,
matching Pusher +24.186 s, first later GET +40.273 s. There was no matching Pusher
for 20 s after ACK. HA's immediate display was optimistic. One queued INFO escaped
between the nominal pause and actual PUT, so the full-window acceptance correctly
failed. That INFO completed before the PUT; the subsequent post-PUT interval had
no LAB INFO/GET. Do not relabel the original run as fully passing after fixing
its gate implementation.

The observation is consistent with INFO-associated delivery, but does not prove
causality. A backend timer with an unknown period, delayed prior work, a GET
side effect or another client may affect delivery. We cannot stop or observe
other clients' INFO merely by pausing this LAB. Missing delivery within a finite
window is a censored latency observation, not proof that spontaneous delivery
never occurs. Minimum-limit delivery does not establish scene or Sleep behavior.

## Fixed experiment sequence

Each row needs its own new consumed session grant and exact numeric approval.
Only `min_temp_unoccupied` is eligible, on the privately selected device with
OFF/Home preserved. Re-read original value and limits for every new session;
choose +1 °C within 12..22 °C, otherwise -1 °C, and freeze its exact restoration.
There is no approval to start a physical trial in this document.

| Trial | Protocol | No-read observation after PUT ACK | Next phase | Real status |
| --- | --- | --- | --- | --- |
| 1 | INFO first | 45 s | Only ordinary INFO, two cycles and 5 s for events; then GET | Pending new numeric/session approval |
| 2 | GET first | 45 s | Exactly one GET, INFO remains blocked for another 20 s; then INFO | Pending |
| 3 | Spontaneous | 80 s | Ordinary INFO first, then GET | Pending |
| 4 | GET first repeat | 45 s | Same as trial 2 | Pending |
| 5 | INFO first repeat | 45 s | Same as trial 1 | Pending |

No next trial starts until the previous original value and OFF/Home are
corroborated. The series permits at most ten PUT attempts, two per session.
Restoration events are recorded separately and are not equivalent repetitions
unless isolation conditions match. Unknown/ambiguous writes stop the series;
there is no automatic replay or automatic restoration after ambiguity.

The grant's optional `experiment` is `info-first`, `get-first` or `spontaneous`;
it is allowed only for a minimum-field PUT and is included in the exact approval.
The independent process requires an OFF baseline for these profiles. The default
legacy scene fixture is not permission for this experiment.

## Admission and timing controls

Persistent INFO and GET barriers exist from gateway construction, before login
or scheduler work. They are acquired synchronously inside the supervisor budget
lock, **after** spacing waits and immediately before transport delegation.
Pausing prevents already queued requests from acquiring a lease, drains delegated
requests, and lets blocked waiters release the budget lock. Pusher remains open.
A GET-first permit admits exactly one GET, including competing queued refreshes;
all further GETs and all INFO remain blocked during that probe's observation.
Neither production source nor production scheduling is modified.

Record actual transport admission separately from HA service invocation and
HTTP response. A no-read observation starts after PUT ACK, but the full isolation
check also covers pre-ACK PUT I/O and any Pusher received before ACK. GET response
status, raw received fields and actually applied snapshot fields are distinct.
A retained Pusher value is never labelled GET-applied when the field was fenced
out of that snapshot. Restoration requires a newly completed GET containing the expected raw field
and matching raw SyncManager state, rather than optimistic state alone. Revision
fencing may correctly retain an identical Pusher value instead of applying that
GET field; the trace keeps that distinction.

The unchanged limits are 285 s active / 300 s parent, 40 state attempts, two
authentication attempts, one socket, no reconnect and 10 s state-start spacing.
After draining and before the first PUT, require at least 175 / 215 / 210 s
remaining for INFO-first / GET-first / spontaneous respectively. If unavailable,
stop before the first PUT. These reserves fit the fixed observation phases and
normal measured response times; they do not guarantee restoration under arbitrary
server delays. A 30 s request timeout, failure or deadline exhaustion can still
require owner intervention. Never extend limits to force a run through.

## Synthetic validation

The exact production integration is copied byte-identically into an isolated HA
process against authenticated loopback TLS/WSS. Only fixture runs compress the
45/80 s waits to 0.5 s and INFO event tail to 0.3 s; real profiles use the fixed
wall-clock durations above.

Four independent HA acceptance cases passed on the current pinned HA baseline:
INFO-first with INFO-triggered fixture Pusher, GET-first with a new snapshot,
GET-first with a stale snapshot until INFO, and spontaneous fixture publication.
Each used exactly two PUTs, confirmed restoration and resource cleanup, had no
reads in isolation and no GET during its INFO-only phase. In GET-first, exactly
one GET admitted. These are synthetic contract checks, not DKN Cloud evidence.

Focused transport tests reproduce the actual pre-existing budget waiter race,
independent gates, delegated-response drain, cancellation and one GET permit with
multiple queued readers. Synthetic ambiguous outcomes cover a server-accepted PUT
with lost response and an undelivered PUT; the durable slot is consumed once and
restoration/replay stays revoked. A stale raw GET cannot confirm an optimistic
or retained Pusher value. INFO-associated fixture publication followed by GET,
and failed INFO/no-Pusher with GET readback, are tested separately. None of these
runs intentionally creates a timeout against real equipment.

## Comparison and decision boundary

For each approved row, save an anonymous timeline and record: last pre-PUT INFO,
PUT admission/ACK, no-read duration and violations, first raw changed Pusher,
GET raw/applied value, INFO/GET admission and response deltas, restore attempt,
restoration telemetry, outside-client status and cleanup. Compare both repeated
orders. Keep trials with isolation violations visible and separate from accepted
ones; no silent reruns. Capture all preceding device events to examine possible
periodicity without assuming a timer or matching it post hoc from five samples.

The first new real trial has been authorized; the remaining four still need
separate approvals. Results are added only after each session closes. Keep recovery/fallback and the
10 s production cadence unchanged while the evidence and independent Astra
review remain incomplete. No merge, release or production HA update is authorized.


## Final localized checks before trial 1

- 72 affected transport/grant/barrier/legacy tests passed once; no full-suite repeat.
- 47 focused evidence, hygiene and policy checks passed after the provenance fix.
- The stale GET-first HA case passed on HA 2026.11.1 and independently on 2026.11.0.
- The [full 80 s synthetic trace](197-synthetic-80s.json) passed standalone with
  unchanged LAB deadlines, no isolated reads, no GET in the INFO-only phase,
  two controls and confirmed restoration/cleanup. It crosses expiry/quiet-age
  thresholds without claiming that a specific watchdog callback was observed.
- A first attempt to put that long wall-clock run in ordinary pytest hit its
  separate 180 s worker ceiling. The default pytest ceiling was not weakened;
  the full-duration case uses the existing standalone launcher instead.
- The CI-specific missing-TMPDIR test assumption was reproduced and fixed by
  declaring its temporary environment explicitly; 11 grant tests passed with
  TMPDIR absent from the invoking shell. The original protection remains.

A stronger intermediate confirmation check intentionally failed when raw GET
correctly matched the restored value but revision fencing retained identical
Pusher state. The final check, independently reviewed by Astra, requires matching
raw GET and raw manager state; it never labels a fenced field GET-applied or uses
an optimistic display as confirmation. Initial failures remain distinct from
these final successful localized runs.
