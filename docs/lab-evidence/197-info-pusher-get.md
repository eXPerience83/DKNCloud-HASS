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
| 1 | INFO first | 45 s | Only ordinary INFO, two cycles and 5 s for events; then GET | [Passed; restored](197-trial-1-info-first.json) |
| 2 | GET first | 45 s | Exactly one GET, INFO remains blocked for another 20 s; then INFO | [Passed; restored](197-trial-2-get-first.json) |
| 3 | Spontaneous | 80 s | Ordinary INFO first, then GET | [Passed; restored](197-trial-3-spontaneous.json) |
| 4 | GET first repeat | 45 s | Same as trial 2 | [Passed; restored](197-trial-4-get-first.json) |
| 5 | INFO first repeat | 45 s | Same as trial 1 | [Passed; restored](197-trial-5-info-first.json) |

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

Accepted real results are listed in the table above and below. Each session
requires separate approval; no permission is implied by this report. Results are
added only after each session closes. Keep recovery/fallback and the
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


## Real trial 1: INFO first

The owner approved this session's exact 16 → 17 → 16 °C pair and reported that no
other clients would query. The LAB cannot independently inspect other clients.
A new authenticated baseline and prior Pusher events confirmed OFF/Home and 16.
The two PUTs returned HTTP 204; exactly two durable slots were consumed.

Relative to first PUT transport admission:

| Seconds | Real observation |
| ---: | --- |
| -20.084 / -20.012 | Last pre-PUT INFO admission / HTTP 201 |
| -9.687 | Read gates drained; HA service invoked |
| 0 / +0.069 | PUT admitted / HTTP 204; optimistic 17 publication follows |
| +40.034 | HA display returns to raw 16 as optimism expires; not Pusher |
| +45.071 | End of 45.003 s after ACK with no LAB INFO/GET or matching Pusher |
| +45.072 / +45.279 | First resumed INFO admitted / HTTP 201; GET still blocked |
| +55.281 / +55.338 | Second resumed INFO admitted / HTTP 201 |
| +55.750 | First actual Pusher min_temp_unoccupied=17, before any GET |
| +60.347 | GET gate resumed |
| +65.283 / +65.393 | First later GET admission / received raw 17 |
| +85.296 / +85.380 | Verification recovery GET admission / raw 17 |
| +105.329 / +105.406 | Restoration PUT admission / HTTP 204 |
| +115.307 | Ordinary INFO after restoration |
| +116.884 | Pusher receives 16 |
| +125.309 / +125.389 | Final GET admission / raw 16; OFF/Home checks pass |

The first matching Pusher arrived 10.678 s after the first resumed INFO admission
and 0.469 s after the second. Unlike the earlier nominal-window failure, this
full interval passed its read-isolation check; the INFO-only phase had no GET.
There was no spontaneous matching update during 45 s after ACK. This is one
accepted INFO-first run, not evidence of a universal timer or INFO causality.
The second INFO, unknown backend periodicity and delayed previous work remain
possible timing explanations. GET-first and repeated trials remain pending.

All resources closed, no pending HTTP, no owner intervention required. At this checkpoint the series had used 2/10 physical PUT attempts; this trial's
consumed grant cannot restart.


## Real trial 2: GET first

A new separately approved session revalidated 16 °C/OFF/Home. Both minimum-field
PUTs returned 204; read isolation, single GET admission and later INFO-only phase
checks passed. Owner-reported quietness of other clients applies to this series.

| Seconds from first PUT admission | Real observation |
| ---: | --- |
| -20.073 | Last pre-PUT INFO; HTTP 201 at -20.013 |
| 0 / +0.068 | PUT admitted / HTTP 204, followed by optimistic 17 |
| +40.038 | Optimism expiry publishes retained raw 16; no Pusher |
| +45.075 | End of 45.006 s after ACK with no LAB INFO/GET or matching Pusher |
| +45.075 / +45.296 | The single GET admitted / raw response contains 17 |
| +45.300 | That GET field is applied and HA publishes 17 |
| +65.334 | GET observation ends; no matching Pusher or INFO during it |
| +65.335 / +65.518 | INFO resumed / HTTP 201 |
| +67.785 | First Pusher min_temp_unoccupied=17 |
| +75.521 | Second INFO |
| +85.524 / +105.536 | Subsequent recovery/verification GETs each return raw 17 |
| +125.547 / +125.609 | Restoration PUT admission / HTTP 204 |
| +135.552 | Ordinary INFO after restoration |
| +137.524 | Pusher min_temp_unoccupied=16 |
| +145.553 / +145.633 | Final GET / raw 16; OFF/Home checks pass |

The GET demonstrably obtained the updated cloud value with **no post-PUT LAB
INFO**. No matching Pusher arrived during the following 20 s GET-observation
window. Matching Pusher arrived 2.450 s after resumed INFO, 22.489 s after raw
GET receipt. This separates raw GET snapshot visibility from Pusher delivery;
it does not establish physical application or prove that INFO caused the event.
A periodic backend update or delayed work is still an alternative explanation.

Restoration and cleanup passed with no ambiguity or replay. The series has used
4/10 attempts. Later trials are recorded separately below; this consumed grant cannot restart.


## Real trial 3: 80 s spontaneous observation

Fresh exact approval and preflight again fixed 16 → 17 → 16 °C on OFF/Home.
Both PUTs returned 204 and all isolation/restoration/cleanup checks passed.
There was no matching Pusher field for **80.011 s after ACK** and no LAB INFO/GET
throughout the full isolated interval. This extends the observed no-event window;
it does not rule out a longer or differently phased independent backend timer.

| Seconds from first PUT admission | Real observation |
| ---: | --- |
| -20.074 | Last pre-PUT INFO |
| 0 / +0.071 | PUT admitted / HTTP 204; HA optimistic 17 |
| +40.050 | Optimism expiry publishes retained 16, not Pusher |
| +80.082 | Isolation ends; INFO resumes |
| +80.082 / +80.283 | First INFO admission / HTTP 201 |
| +90.285 / +90.361 | Second INFO admission / HTTP 201 |
| +95.413 | GET gate opens, but no GET has yet been sent |
| +96.684 | First actual Pusher17 |
| +100.285 / +100.384 | First post-PUT GET / raw 17 |
| +120.287 / +120.396 | Verification recovery GET / raw 17 |
| +140.294 / +140.363 | Restoration PUT admission / HTTP 204 |
| +150.289 | Ordinary INFO after restoration |
| +153.815 | Pusher16 |
| +160.291 / +160.383 | Final GET / raw 16 and OFF/Home checks pass |

The event arrived 16.601 s after the first resumed INFO, 6.399 s after the second,
and **3.602 s before the first GET admission**. The fixed INFO-only observation
flag is false because its phase ended at GET-gate opening, 1.270 s before this
event. That coarse flag is not absence before actual GET: the monotonic
transport timeline establishes the ordering. Do not attribute the event to
opening a gate, which sends no request. Source behavior and deadlines were not
changed or extended during this trial.

The series has consumed 6/10 attempts, all restored and closed. Other-client
quietness remains owner-reported. The subsequent separately approved trials are recorded below.


## Independent review after trials 2 and 3

Astra independently confirmed that trial 2 establishes cloud snapshot visibility
without prior post-PUT LAB INFO, not physical application or the fate of a lost
HTTP response. Matching readback may aid ambiguous-outcome investigation; a stale
mismatch cannot prove an undelivered command, and neither permits replay.

For trial 3, Astra confirmed that the fixed phase flag is not a substitute for
transport order: Pusher arrived after two INFO admissions and before any GET
admission. Its variable lags and the finite no-event window establish neither
one triggering INFO nor a fixed backend refresh period. Keep the production
0.6.0 policy unchanged until the repeated comparisons and final review.


## Real trial 4: GET-first repeat

A separate numeric/session approval and fresh OFF/Home/16 °C preflight repeated
trial 2. No matching Pusher or LAB read occurred during 45.005 s after ACK.
The one GET was admitted at +45.071 s and received raw 17 at +45.283 s, with
no post-PUT INFO. No matching Pusher arrived during the following 20 s.
INFO resumed at +65.327 s (201 at +65.529); first Pusher17 arrived +68.561 s,
3.234 s after INFO admission and 23.278 s after raw GET receipt.

Verification GETs at +85.536 and +105.543 returned 17. Restoration PUT at
+125.552 returned 204 at +125.620; INFO +135.557 preceded Pusher16 +138.904.
Final GET +145.558 returned raw16 +145.651, with OFF/Home/mode checks passing.
Both PUTs returned 204, isolation/single-GET/INFO-only checks passed, resources
closed, no ambiguity or replay. The series has used 8/10 attempts. The separately approved final trial is recorded below; its two attempts were
the last available in this series.

The two GET-first repetitions agree: GET saw updated cloud state before INFO,
without a Pusher event during the next 20 s; matching Pusher followed INFO.
This consistency is stronger than a single timing coincidence, but delayed GET
side effects and backend periodicity are not excluded by two finite windows.


## Real trial 5 and final comparison

The last separately approved session revalidated OFF/Home/16 °C and used only
its final two authorized attempts. PUT 17 returned 204 at +0.068 s. There was no
matching Pusher or LAB INFO/GET for 45.006 s after ACK. INFO resumed at +45.074 s
(201 at +45.277); Pusher17 arrived +47.154 s, 2.080 s after that INFO. The first
GET at +65.284 returned raw17. Restoration PUT +105.302 returned 204 +105.381;
ordinary INFO +115.305 preceded Pusher16 +117.618. Final GET +125.306 received
raw16 +125.391 and OFF/Home checks passed. No ambiguity/replay occurred; the
process exited 0, temporary configuration was removed and no HTTP tasks remained.

All five complete anonymous timelines are linked in the table above. Structured
comparison: [JSON](197-series-summary.json) / [CSV](197-series-summary.csv).
Times below start at each trial's actual transport admission, not HA invocation.
All intervals use the same monotonic source within that independent session.

| Trial | Protocol | No-read seconds after ACK | GET17 before any INFO | First Pusher17 after PUT | Lag after first / last preceding INFO | Restored |
| --- | --- | ---: | --- | ---: | --- | --- |
| 1 | info-first | 45.003 | Not probed | 55.750 s | 10.678 / 0.469 s | Yes |
| 2 | get-first | 45.006 | Yes | 67.785 s | 2.450 / 2.450 s | Yes |
| 3 | spontaneous | 80.011 | Not probed | 96.684 s | 16.601 / 6.399 s | Yes |
| 4 | get-first | 45.005 | Yes | 68.561 s | 3.234 / 3.234 s | Yes |
| 5 | info-first | 45.006 | Not probed | 47.154 s | 2.080 / 2.080 s | Yes |

**Observed:** no matching Pusher appeared during the five no-read windows, including
80 s. Both GET-first repetitions obtained raw17 without any post-PUT LAB INFO;
neither produced matching Pusher during its following 20 s INFO-free observation.
All five first matching Pusher events followed INFO resumption. In the three
INFO-first/spontaneous trials, Pusher preceded any post-PUT GET. The series used
exactly ten PUT attempts, all 204; all five originals were corroborated restored,
all remained OFF/Home, and all five sessions closed. No additional real operation
or experiment is authorized by completion of this series.

**Limits:** this supports INFO-associated delivery, not a proven triggering
mechanism or universal latency. A delayed GET effect, earlier work or independent
backend refresh with an unknown interval is not excluded. Absence for 45/80 s is
right-censored. The owner reported no other clients; the LAB did not independently
monitor outside traffic. These are one device/account, one root-level minimum
field, OFF/Home and successful HTTP responses, not scene/Sleep/P7/P8 or physical
convergence evidence. No real timeout was induced. GET17 shows cloud-reported
state, not reconstructing a lost ACK or proving hardware application.

Each session has a separate elapsed-time origin and observation gap; the reports
do not provide a continuous cross-session record or shared absolute origin from
which to fit a backend timer. Ordinary later event spacings also reflect the
LAB's 10 s global request budget and interleaved GET/control traffic; do not
mistake those for a server refresh period. Trial 3's gate-open/HTTP-send distinction
is documented above. Restoration phases ran ordinary synchronization and are
not five additional independent trials.

No production policy was changed. No full suites were repeated locally. The
code commit **58c7a7d** passed all seven GitHub workflows, including **996 tests**
on each pinned HA version and coverage gates: [current HA](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43767203266),
[minimum HA](https://github.com/eXPerience83/DKNCloud-HASS/actions/runs/43767203274).
This final evidence update changes documentation/data only. The owner stopped
further review/investigation after trial 5; final Astra assessment of all five
runs is deferred. See [the handoff](197-astra-handoff.md).
