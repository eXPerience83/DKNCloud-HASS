# Post-trial INFO / Pusher / GET synchronization assessment (#197)

Status: **read-only architecture review**, 2026-11-15. This is a recommendation
for 0.6.0, **not** vendor-contract proof, an approved release, a production
change, or the deferred final Astra audit. No live requests or writes were
performed for this review.

## Sources and scope

- Complete five-trial, ten-PUT real series: [197-info-pusher-get.md](197-info-pusher-get.md),
  [structured summary](197-series-summary.json) and the five linked monotonic
  traces. Source-under-test: `58c7a7dcf07d4f232ccbe22f70df1764f92aba91`.
- Current `main` at `635720037b7a2a17debb57e2497ed0b6e359156d`:
  `sync_manager.py`, `sync_scheduler.py`, `sync_state.py`, `helpers.py`,
  `number.py`, `climate.py`, `airzone_api.py`, `__init__.py`, `info.md`.
- [PR #222](https://github.com/eXPerience83/DKNCloud-HASS/pull/222),
  reviewed exact HEAD `6ca8e14115a2e6a92c4f422da489e068e6c1753b`;
  [independent PUT audit](https://github.com/eXPerience83/DKNCloud-HASS/pull/222#issuecomment-6898507222).
- Historical user observation: external **physical-panel** changes could remain
  stale in GET-only HA for several minutes, then update when the official app
  was opened (which appeared to issue INFO). This is a user report, not a
  controlled physical-panel A/B trial.

## Observed behavior, narrowly stated

The five separately approved sessions each wrote
`min_temp_unoccupied: 16 -> 17 -> 16` on one OFF/Home device.
All **10 PUTs returned HTTP 204**. The original value and OFF/Home
were corroborated by raw GET and received Pusher for every restoration;
no ambiguous result, automatic replay, physical power-on or leaked LAB task.

| Trial | After first PUT ACK | GET 17 before post-PUT INFO? | First matching Pusher 17 |
|---|---|---|---|
| 1, INFO-first | No LAB INFO/GET or matching Pusher for 45.003 s | Not tested | +55.750 s; after two resumed INFOs, before GET |
| 2, GET-first | 45.006 s silent | **Yes**, raw GET at +45.296 s | +67.785 s; 2.450 s after INFO resumed |
| 3, long isolation | 80.011 s silent | Not tested | +96.684 s; after two INFOs, **before first GET send** |
| 4, GET-first repeat | 45.005 s silent | **Yes**, raw GET at +45.283 s | +68.561 s; 3.234 s after INFO resumed |
| 5, INFO-first repeat | 45.006 s silent | Not tested | +47.154 s; 2.080 s after INFO resumed |

In both GET-first trials, a *single* GET returned new cloud data without a
post-PUT LAB INFO, followed by **20 s without a matching Pusher** while INFO was
still blocked. All five matching Pusher updates arrived after resuming INFO;
in the INFO-first/long-isolation experiments they occurred before any actual
post-PUT GET. The earlier 20 s experiment had an invalid full-window marker
because one queued pre-PUT INFO crossed the initial gate; its raw post-PUT
interval is usable only with that limitation. The new five-trial gate was
regression-tested and its admission intervals passed.

**Interpretation:** INFO-associated Pusher delivery is strongly supported.
GET and Pusher expose different observations: GET can return an already-updated
cloud snapshot without causing a matching push within 20 s; Pusher can distribute
state after INFO and without a GET. INFO *causing* a refresh or triggering
Pusher is a plausible explanation, **not proved**. Unknown cloud periodicity,
delayed work, unobserved external-client INFO/GET, and the short finite windows
remain alternative explanations. Do not claim a measured periodic refresh
interval from independent sessions. GET 17 is cloud state, not proof of
physical application; the tested field is not a physical-panel state change.
Do not extrapolate the result to every scene/Sleep PUT or P1/P2/P7/P8.

## Version-specific architecture

- Published `v0.4.8` and `v0.5.0a6`: coordinator GET polling, with
  nominal 10-30 s configured scan interval; a successful write schedules a
  coalesced coordinator refresh about 1 s later. Optimistic values persist
  until their adaptive TTL (at least scan interval + 0.5 s) expires.
  Fast or repeated GET is not guaranteed to reflect a **physical-panel** update
  if the vendor cloud snapshot itself lags.
- Current `0.6.0a1`: initial GET snapshot, nominal **10 s INFO per device**
  while Pusher subscribed, **Pusher as primary remote state channel**, no
  unconditional GET following successful PUT when `SyncManager` exists.
  Its optimistic overlay has a **30 s TTL**; matching explicitly received
  fields retire overlays, unrelated patches do not. Expiry requests a coalesced
  recovery read. The scheduler falls back to spaced INFO and GET on loss of push;
  subscription resync performs GET; a 60 s quiet-channel watchdog can request
  recovery. Actual schedules can be delayed by failures, cooldown and budgets.
- `SyncState` uses per-field revisions and connection generations so a
  GET started before a newer push does not overwrite that newer field.
  A GET accepted by HTTP is **not** evidence of physical freshness.
- The design is not 'pure push': INFO is active periodic polling of the
  vendor service with results delivered via Pusher. It is not proven to save
  more requests than GET-only polling. Its practical value is the suspected
  ability to solicit fresh device data and distribute physical-panel changes.

## 0.6.0 policy recommendation: smallest justified change is no change

**Successful PUT (HTTP 2xx):** keep bounded optimistic HA publication and the
ordinary 10 s INFO/Pusher schedule. Do **not** add an unconditional post-write
INFO or GET; neither lower the 10 s interval nor extend the 30 s overlay from
the deliberately isolated LAB delays. Pusher must match the actual changed
field before clearing its overlay; otherwise expiry and budgeted recovery
remain available.

**Ambiguous PUT (timeout, cancellation, possibly accepted 5xx):** propagate
uncertainty, **never replay the command**, and request a coalesced GET via the
existing recovery path. The two GET-first trials show an early GET *can* be
informative before INFO, so delaying **every** recovery GET until the next
INFO is not supported. A matching GET corroborates cloud-reported state,
not receipt of a lost HTTP response or physical application. A stale
nonmatching GET does **not** prove the write was unsent; retain uncertainty
and ordinary INFO/Pusher. A known `CommandNotSentCooldown` should not trigger
ambiguous-command recovery. Do not treat INFO success as confirmation of
any individual field.

**Pusher interrupted or silent:** retain existing connection-generation
fencing, resync GET, INFO/GET fallback budgets and cooldown/backoff. Keep
GET recovery available even if INFO fails. Avoid extra duplicate polls and
never let an older GET overwrite newer received Pusher fields.

**Scenes/Sleep:** retain scene-intent and per-device locks, Sleep exit/rearm
pending fences, and local session protections. Do not infer that an accepted
scene PUT always produces Pusher delivery or that a scene GET proves a
physical Sleep transition.

## PR #222 assessment and remaining acceptance

The PR addresses a confirmed, narrower defect in *ambiguous ordinary scene
PUTs*, and also fences late successful PUT completion after entry
unload/replacement across scene/number/automatic cleanup paths. Its patch
does **not** change INFO/Pusher cadence, GET recovery algorithm or optimism
TTL. The observed GET-first result supports **preserving**, not removing,
its coalesced recovery. The PR's established Sleep-exit protections remain
distinct from ordinary Home/Away/Sleep-entry failures.

Reviewed `6ca8e14`: **7/7 exact-HEAD Actions passed** (tests, independent
minimum HA, lint, HACS, hassfest, package, workflow security); open and
GitHub reports mergeable; **zero inline review threads and zero submitted
reviews**. CodeRabbit **skipped auto-review**; do not mislabel it an approval.
An earlier independent diff/source/test audit and focused regressions are
recorded in [the PR discussion](https://github.com/eXPerience83/DKNCloud-HASS/pull/222#issuecomment-6898507222).
The five real trials tested **successful numeric field PUTs**, not physical
ambiguous scene errors. They do not require a change to #222.

Before integration: (1) maintainer review/approval of this bounded policy and
any desired additional CodeRabbit/manual review, (2) recheck the **then-current**
PR head, base/mergeability and required exact-head CI, (3) **explicit
maintainer authorization** before squash merge. No real scene timeout,
physical Sleep acceptance, HACS migration or release is required to validate
this targeted safety fix. Keep #197 open for remaining broader acceptance.
Do not merge #223's LAB harness as a prerequisite.

## Non-blocking future physical-panel experiment

**Backlog only; not authorized or scheduled.** The main remaining vendor
question is how long an **external physical-panel** change takes to become
visible with ordinary INFO/Pusher active versus INFO temporarily paused,
and whether GET remains stale in either condition.

If later worth testing, adapt the existing independent LAB to a
**read-only authenticated observer** with independent INFO/GET gates and a
Pusher timeline. Establish a fresh baseline, subscription ACK, raw power/mode
and cloud snapshot. The **owner manually** makes one reversible panel change
(when safe and appropriate), records the physical action time, and manually
restores the original state; no LAB control commands. Compare two separately
authorized observation windows:
1. INFO normal at 10 s, receive raw Pusher field/HA update and optional GET;
2. INFO paused while GET remains independently observable, then INFO resumed,
   recording raw GET/Pusher and actual HTTP admissions.

For causal attribution, avoid concurrent official-app/HA clients if the owner
can do so, record any uncontrolled clients and clock uncertainty, match the
same physical change and initial state, and distinguish GET's cloud snapshot
from actual hardware telemetry. Protect against long unknown device states:
no forcing power/mode/Sleep, no unsafe automatic restoration; the owner
decides a suitable physical-panel action. Keep the default real-target denial
and all authenticated TLS, session-bound grants, quota, teardown and privacy
controls. Prepare a new explicit approval for live access and physical
interaction on that later day. **Not a blocker for #222 or 0.6.0 safety fixes.**

## No action taken

This assessment only documents existing source, real evidence, remaining
uncertainty and recommended policy. It sends no INFO/GET/PUT, requests no
credentials, and changes no production logic or CI requirements. Any future
implementation or release must be separately authorized and tested.
