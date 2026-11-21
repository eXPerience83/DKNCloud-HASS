# Sleep backend evidence and target HA policy — #197 / #234

**Status:** ACCEPTED DESIGN for subsequent implementation review, **not implemented by this document**.
**Owner decision:** 2026-11-21. PR #234 remains unmerged; this document changes no runtime behavior.
**Scope:** DKN Cloud Sleep timer, power ON/OFF and automatic scene cleanup. Keep unrelated features and LAB implementation separate.

## 1. Immutable evidence and limitations

### Original real experiments

[Real supervised NAS Sleep report, commit `2e91d01`](https://github.com/eXPerience83/DKNCloud-HASS/blob/2e91d01/docs/lab-evidence/236-sleep-live-results.md) is the source of truth. A single physically supervised unit used FAN_ONLY P2=3, low fan, a verified five-minute Sleep value, one private Pusher subscription, INFO every 10 seconds, scoped GET, and separate physical observations. Every session had separate authorization. All experiments **omitted occupied writes during the measurement** and restored OFF, occupied and 30 minutes after closing. The initial aborted duration-only attempt was **not** a Sleep trial.

| Experiment | Commands / action | Backend and physical evidence | Inference |
|---|---|---|---|
| A, Sleep selected with power OFF | t=0 successful scene Sleep; P1 ON t+240.08 s | GET t+300.09 s still ON; first OFF PUSH t+311.10 s, scene Sleep; physical OFF corroborated | Compatible with timer anchored to Sleep selection, not ON |
| C, Sleep while ON, then remote OFF | P1 OFF t+120.06 s | First OFF PUSH t+129.32 s; GET t+390.08 s still OFF and Sleep | Remote OFF does not itself switch reported scene to occupied; internal timer outcome after OFF unknown |
| B revised, Sleep while ON, physical OFF then remote ON | Operator panel OFF prompted t+30 s; P1 ON t+240 s | First OFF PUSH t+53.13 s; pre-wake GET t+220.09 s OFF/Sleep; first re-ON PUSH t+248.81 s; GET t+300.08 s ON; first later OFF PUSH t+312.61 s, Sleep; physical OFF corroborated | Panel OFF and remote re-ON **before** expiry did not restart the apparent countdown |

PUSH/GET timestamps represent delivery/receipt and need not coincide with physical switching. The evidence does **not** prove that the timer is cloud-side versus inside the controller, exact physical deadline, scene change on expiry (the backend continued to report Sleep), or **rearm after the first natural Sleep OFF**. The proposed F experiment was **not run and is not authorized**; it is not a prerequisite for implementing the chosen HA cleanup policy.

### Independent HA corroboration

The owner's sanitized HA history cross-check is in [issue #236](https://github.com/eXPerience83/DKNCloud-HASS/issues/236#issuecomment-7069667905), original CSV retained privately (not committed because it includes identifying entities). It shows:
- A: Sleep t=20:59:39.207 UTC; HA ON t=21:03:42.165; HA OFF t=21:04:45.143 (~306 s after scene).
- C: Sleep 21:09:50.196; HA OFF 21:11:56.157.
- B revised: Sleep 21:27:58.897; physical-panel-related HA OFF 21:28:49.282; HA ON 21:32:04.966; HA OFF 21:33:08.769 (~310 s after scene).
- Home and 30-minute restoration appeared **later, after the measurement**, not as spontaneous backend transitions.

HA `last_changed` is not physical switching time nor proof of which transport delivered a change. The NAS trial report supplies the separate PUSH/GET/physical provenance.

### Prior evidence (do not repeat)

- [Draft LAB #223](https://github.com/eXPerience83/DKNCloud-HASS/pull/223) and its [five accepted INFO/Pusher/GET trials](https://github.com/eXPerience83/DKNCloud-HASS/blob/859bd094c1c6918690311a46c187a06a8b404b2f/docs/lab-evidence/197-info-pusher-get.md) show that a GET can contain the newly accepted cloud field before matching Pusher, while matching Pusher arrived after INFO resumed in those trials. INFO-causality and universal timing were **not proved**. Hence do not assert PUSH always wins or GET is always authoritative/fresh.
- Earlier #190 Sleep trials showed that explicit guarded occupied cleanup made a later one-shot Sleep cycle work. This is an observed integration outcome, not a backend guarantee.
- [Consolidated LAB ledger #236](https://github.com/eXPerience83/DKNCloud-HASS/issues/236#issuecomment-7069634138) covers chronology, safety limits and future reusable LAB work.

## 2. Selected integration behavior

**Rule:** Sleep is a backend-controlled countdown initiated by a **new accepted selection** of `scenary=sleep`. The countdown continues across intermediate physical/remote OFF and ON, as tested in A/revised B. HA must **not** treat an OFF before the timer deadline as cancellation or perform an early automatic `occupied` cleanup. HA must not implement another P1 shutdown timer, issue P1 OFF by wall clock, or silently rearm Sleep on power toggles. After the **expected timer deadline** plus conservative settling, and only with verified OFF and current scene/session, enabled HA may restore `occupied` **without changing P1**. The backend may keep raw `scenary=sleep` even after timer expiry. Explicit manual Home/Away remain immediate overrides.

### 2A. Selecting Sleep

- When OFF + occupied: a user's Sleep selection writes Sleep; **no P1 ON**, and OFF observations belonging to the initial OFF state are **not** mistaken for a newly completed ON→OFF transition.
- When ON + occupied: select Sleep; retain ON.
- Keep the server-accepted Sleep selection timestamp, expected `sleep_time`, scene revision and original ownership for the current session. A local command acknowledged by the backend and an externally discovered scene are **not equally precise timing evidence**. Do not mark the timer start as known merely because HA first saw Sleep in a delayed PUSH/GET after startup.
- Mode/fan/setpoint edits during active Sleep preserve its scene. Changing Sleep duration during a running session invalidates calendar-based expiry inference; do not assume the backend rescheduled it.
- **Idempotent Sleep selection while the current session is still active:** if the user selects Sleep again and the current Sleep timer has not been confirmed finished/cancelled, do nothing: **no scene PUT, no `occupied → sleep`, no P1, no new t0, no new generation or persistence write**. This includes a machine currently OFF during a Sleep session started OFF (real trial A), or a machine temporarily OFF before its known timer deadline.
- **Rearm only a confirmed finished/cancelled Sleep session:** if Home has already been restored, a new Sleep selection sends Sleep normally. If raw `scenary=sleep` persists from a *verified ended* session (e.g., cleanup disabled or unsuccessful), a genuinely explicit request to begin **a new** Sleep session may use serialized `occupied → confirmed → sleep` without changing P1. Never classify raw Sleep+OFF alone as 'ended' or force this two-PUT transition for a still-active or uncertain session.
- **Unknown timer/session identity:** same-preset Sleep selection does not implicitly reset a possibly active timer. Expose unknown/estimated timing rather than rewriting the scene; a user who intentionally wants to abandon an unknown session can first choose Home and then Sleep, with the standard guarded scene writes. A P1 ON alone never rearms Sleep.

### 2B. ON while Sleep is still active

- If raw/guarded power is already confirmed ON, **do not send a redundant P1 or cancel Sleep**.
- If power is OFF for **any reason during a still-active Sleep countdown** (selected while OFF, manually switched OFF, or remote P1 OFF), an explicit HA ON sends only the necessary P1 ON, **retaining Sleep and the original deadline**; matches A and revised B. Do not classify OFF alone as a finished session.
- After a Sleep session has **actually been cleaned to occupied**, a later HA ON operates normally (no old countdown). If the trustworthy deadline has elapsed but occupied cleanup is still awaiting fresh OFF evidence, distinguish `expired_unverified` from `cleaned`: do not label the session finished from wall clock alone. For an explicit HA ON from confirmed OFF after a verifiably completed expiry, serialize any pending occupied scene restoration before P1; on ambiguous scene outcome, do not assume success or replay commands.
- If the current Sleep start/deadline is **unknown** (HA restart, delayed external scene), an explicit HA ON from confirmed OFF **preserves the raw Sleep scene** rather than automatically canceling what may still be an active countdown. Observe it and use the first-valid-observation conservative bound (§7) for eventual cleanup. Surface the remaining time as uncertain / conservative, not as a precise backend timer. An explicit user Home/Away can still cancel immediately. This choice preserves native Sleep behavior rather than silently changing scene.
- A **genuine new scene command or new confirmed Sleep session** supersedes earlier scheduled work; an idempotent repeated Sleep selection **must not** bump the session revision, reset stored start/remaining-time or suppress legitimate cleanup. Never resurrect expired Sleep or overwrite a newer manual selection.

### 2C. OFF during Sleep before timer expiry — preserve the countdown

- **OFF is a power state, not a Sleep cancellation.** Natural/manual/remote OFF reports received **before the expected timer deadline** may be recorded and corroborated but **MUST NOT** authorize an automatic `occupied` scene write. This includes panel OFF and remote P1 OFF. The user can turn the machine back ON during the remaining interval, and the backend continues the **original** countdown (A and B evidence).
- HTTP 2xx acknowledgment of P1 OFF is not proof of physical OFF; an ambiguous response must not be replayed. Observe backend power with independent newly received PUSH and/or GET records. Preserve session and start timestamp across intermediate OFF→ON changes.
- At/after the **trusted deadline plus proposed 30 s settling window**, request bounded verification. To prevent a stale pre-expiry OFF being counted as current, require **two independent newly applied OFF observations after the deadline/settling window**, from PUSH **or** GET or mixed, along with raw Sleep, unchanged duration and current session generation, and no later ON/scene intent. Duplicate event, cached maintenance state, one GET forwarded twice and stale snapshots count at most once.
- When eligible, dispatch **at most one** non-retried `scenary=occupied` and **no P1 command**; request bounded scene readback. An HTTP success is pending until scene convergence is observed. Uncertain or superseded writes do not blindly retry.
- If power is still ON at the deadline/settling boundary, **do not** turn it OFF or clean scene while ON; report the existing read-only overdue-power diagnostic only after its separate grace and fresh observations. If OFF arrives late, obtain two fresh OFF reports and then clean, with the same fences.
- Explicit manual Home/Away is a different user action and may cancel Sleep before deadline. Re-selecting Sleep while it remains in progress is idempotent and cannot restart the timer.

### 2D. Sleep started OFF and never observed ON — **new required path**

**This path MUST be supported even though the current code requires `observed_on=True`.** The clock can run without the device being ON (supported by real trial A). The absence of a prior ON must not leave raw Sleep indefinitely, but **the cleanup deadline and fresh OFF criteria are the same as §2C**.

- **Never clean immediately** from baseline OFF or from two OFF observations before the known timer deadline: doing so would cancel a valid Sleep selected while OFF.
- For a **locally acknowledged Sleep selection with known clock and verified unchanged duration**, let `T = accepted Sleep time + sleep_time minutes`. After `T` **plus a small conservative settling window (proposed 30 seconds; implementation should test this as a policy constant, not conflate it with the existing five-minute read-only ON-overdue margin)**, request bounded fresh observation.
- Require two independent **post-deadline-and-settling** OFF reports (PUSH and/or GET, as in §2C), the same current Sleep session and raw scene Sleep, no newer ON or scene selection, and a valid active entry. The 30 s window and corroboration make this a deliberately conservative **HA cleanup policy**, not physical proof that the backend has consumed its timer while always OFF.
- After confirmation send only `scenary=occupied`, no P1 OFF or ON; one attempt, no ambiguous replay, verify scene via readback. A second GET may be scheduled by the existing budgeted recovery if no fresh reports arrive. If they do not arrive, leave Sleep pending instead of creating false evidence.
- If the real start is unknown after restart, **do not invent the backend's original t0**. The optional conservative first-fresh-observation + duration upper-bound policy in §7 can allow eventual OFF-only cleanup; never use that bound as a claim of exact expiry. A changed duration, unavailable backend or ambiguous state blocks unsafe cleanup. Explicit manual Home stays available.
- If any distinct fresh ON appears before cleanup, invalidate the OFF-only candidate. If ON arrives after the expected deadline and remains ON, retain Sleep and surface only the existing read-only overdue anomaly after its own confirmation; do not auto-P1 OFF.

### 2E. Option and manual control

- Keep `sleep_cleanup_enabled`, **default ON**. Enabled = automatically write occupied **only after expected timer expiry plus safe confirmation of OFF**, whether the machine was ON, intermittently OFF or always OFF during the countdown. Disabled = **no automatic occupied write**; expose raw Sleep faithfully and allow manual Home, Away or a true new Sleep session when the previous one is demonstrably over. Selecting Sleep again during a live or uncertain session is idempotent.
- Label/description should be **"Restablecer Home automáticamente al finalizar Sleep"**, without changing the persisted option key; OFF before expiry must never trigger this option.
- Manual Home/Away always outranks delayed cleanup. Never mark Home as verified just because HTTP returned 2xx.
- An explicit HA OFF when already OFF is normally P1-idempotent; it need not manufacture a new ON→OFF transition for the OFF-only case. The user's intentional choice of Home is a separate scene command.

### Original-timer continuation through OFF/ON (owner revision)

The revised B real trial demonstrated that a physical OFF followed by remote ON **before expiry** does not restart the original backend timer. This is now the **selected HA behavior**: do not automatically send `occupied` when OFF is confirmed early. Preserve the initial deadline and allow a later ON to continue the remaining time. Only after the initial timer deadline plus a conservative settle window, and confirmed current OFF, may HA clean the stale Sleep scene. This supersedes the earlier policy that treated any confirmed OFF as immediate cancellation.

**Boundary cases:** if the deadline passes but power still reports ON, no automatic P1 OFF or occupied scene write; use overdue diagnostics and wait for OFF confirmation. If an explicit ON is requested after a known finished/cleaned session, protect ordering so it is not sent beneath stale Sleep. A previously unknown or rearmed timer remains uncertain, and no elapsed-time claim is stronger than its source.

## 3. Safety and race contract

- **No automatic P1 replay**, no automatic retry of ambiguous scene PUT, no 3xx success/redirect replay, no loss of already accepted P1 after caller cancellation. These are the other currently open #234 CodeRabbit findings and must be implemented/audited independently within #234.
- Serialize per device using existing entry ownership, scene revision, preset intent, power-intent generation, locks and pending readback. Do not add a competing global command queue or a second timer that turns power off.
- Do not assume two samples are independent merely because they arrived on different transports; require separate eligible source observations. Do not require PUSH specifically: OFF **PUSH or GET** is sufficient if independently corroborated.
- Refuse stale writes after unload/reload, when a newer Sleep/occupied/away/ON request supersedes, or when a scene exit is unverified. An accepted HTTP scene PUT is `pending` until fresh scene evidence confirms actual occupied.
- An ambiguity should keep the correct pending state, request bounded readback and never silently continue to P1 ON as though the scene had changed. No repeated physical commands after unknown dispatch.
- Avoid using the ordinary ON-overdue diagnostic's `SLEEP_TIMEOUT_GRACE_MINUTES=5` to delay OFF-only cleanup: the diagnostic is read-only and serves a different purpose.
- After HA restart, a durable HA-owned Sleep start may be recovered and revalidated. Raw power/scene telemetry alone cannot reconstruct the original backend t0 nor prove whether an external same-scene rearm occurred; see §7.

## 4. Required changes for #234 (documentation now; code later)

1. **Sleep session derivation** in `__init__.py`: retain a reliable ACK-based local start for the no-ON path. Distinguish ON→OFF corroboration from expiry-while-always-OFF. Do not let merely first-seen external Sleep create a "known" exact deadline. Gate later repeated OFF observations and post-deadline recovery correctly.
2. **Candidate scheduler and guard** in `__init__.py` and `helpers.async_auto_exit_sleep_if_needed()`: apply the **same deadline+settling+two current OFF evidence requirement for all automatic Sleep cleanup** regardless of whether ON was ever observed. Neither physical nor HA P1 OFF before deadline can invoke occupied cleanup. Remove unconditional `tracking.observed_on` eligibility for expired OFF-only sessions; preserve independent evidence and current intent. Never let a stale cleanup override a newer scene or power request.
3. **Climate/Switch ON and preset idempotency**: preserve the original Sleep while a session is active **or its deadline is unknown**; after verified expiry plus scene cleanup, ordinary ON operates in occupied. Share the policy across Climate, direct Switch and delegated Switch→Climate; do not send redundant P1. In `climate._async_set_preset_mode`, the current `terminated_raw_sleep()` predicate (`raw Sleep` + `power OFF`) **wrongly treats an active OFF-only session as terminated**: replace it with session-aware verification of finished/cancelled state. Keep a repeated preset Sleep request truly idempotent (no write, no new timestamp/revision).
4. **Power OFF**: keep idempotent, separate ACK/uncertainty/confirmed backend state and track OFF→ON→OFF under the original Sleep session. Never auto-clean occupied before deadline, including panel and remote OFF; at expiry perform only scene cleanup if still OFF and freshly confirmed. No automatic P1 OFF on timer deadline.
5. **Readback, retries and no-replay**: handle 3xx, 2xx cancellation, accepted vs not-dispatched and ambiguous scene writes, along with unload and newer intents. Avoid forcing a GET if a valid distinct PUSH corroboration already supplies evidence, subject to existing strict recovery budget/field precedence.
6. **Options, diagnostics and documentation**: preserve `sleep_cleanup_enabled` and its default; add time-remaining/end-at diagnostic sensors and local session storage in §7; update `info.md`, translations and tests **when implementation lands**, so documentation reflects actual runtime rather than only this target design.

## 5. Regression matrix before merging #234

- OFF/occupied→Sleep with 0, 1 or 2 fresh OFF before deadline: **no occupied**, no P1.
- Same, two distinct OFF observations **after T+settling window**: one occupied, no P1, verified GET; test PUSH+PUSH, GET+GET, PUSH+GET and duplicate suppression.
- OFF-only with unknown t0, HA reload, changed duration, stale GET, duplicate PUSH, unavailable/auth failure or missed deadline evidence: **no premature occupied**, no replay.
- OFF→Sleep→P1 ON before deadline: retain Sleep and original countdown; no duplicate P1; at expiry two fresh OFF reports after settling enable scene cleanup.
- ON→Sleep→panel OFF at t+2m: **no occupied before t+5m**; panel/HA P1 ON at t+4m preserves original t+5m deadline. Remote P1 OFF at t+2m: same, no occupied until expiry. At t+5m+settle with two fresh OFF reports: one scene-only occupied. Natural OFF around the deadline: same. Test pair independence, session revision and unchanged end timestamp.
- Successful 2xx OFF without OFF observations: **no cleanup**; two OFF observations **before** deadline likewise do not clean. Ambiguous OFF without corroboration: no replay and no false occupancy.
- Early OFF then new ON **before** timer expiry: preserve active Sleep and original countdown; no occupied. OFF at/after expiry, then new ON while occupied cleanup is pending: serialize/check user intent and scene; avoid sending ON under a proven finished but uncleaned old Sleep, and do not assume scene convergence after HTTP alone.
- A genuine new Sleep session while cleanup waits for lock, manual occupied/away and reload/unload: older cleanup makes **zero writes** to newer session/entry. Repeated Sleep while *still active* leaves countdown, generation and queued cleanup unchanged.
- Sleep cleanup option disabled: no automatic occupied in either path; explicit Home/rearm still work.
- Sleep still ON past estimated deadline: **no forced OFF or occupied**, read-only overdue diagnostic remains.
- Test timer-spaced observation for no-ON path at 5min, with test clock/monotonic fake; do not sleep for wall-clock minutes.
- Transport 304/307/308, 2xx followed by caller cancellation, stale overlapping ON/OFF and ordinary Climate/Switch delegation retained as separate #234 blockers.

## 6. Review/implementation gate

The branch for PR #234 now contains an implementation of the approved policy, including the independent expiry deadline, OFF-only path and preserved Sleep on an intermediate ON. This documentation does **not** certify that implementation: the current/minimum HA tests, protected LAB smoke, CodeRabbit and an independent cross-module review must pass for the exact final SHA. Do not merge #234 based only on this document.

The actual physical evidence is preserved in its original lab report on draft #237. Trial F remains **deferred**, no permission for an additional real command is implied. When the code implementation is ready, re-run HA current/minimum, protected LAB smoke, CI and CodeRabbit, then conduct independent whole-series audit of #231–#234 before final closeout of #197.


## 7. Follow-up: time-remaining diagnostics and restart recovery (2026-11-21)

This section records the approved engineering design after the owner chose to **preserve the active Sleep countdown**. Timer diagnostic and HA Store code is now implemented on PR #234 but remains unaccepted until regression tests and final code review pass. Neither the sensors nor persisted estimates are proven backend timer fields.

### 7A. Read-only timer diagnostics

- Per-device diagnostic **Sleep time remaining (estimated)**: integer whole minutes at most one local update per 60 seconds (ceil remaining, clamp to zero); this is **never** an additional cloud poll or command.
- Per-device **Sleep expected end** as a timestamp sensor for UI/automation. Both are local estimates, **not** a backend-provided countdown. If even a conservative bound is impossible, expose unknown/unavailable instead of inventing a deadline.
- Clearly distinguish source/confidence: `local_ack`, `restored`, `startup_upper_bound`; distinguish `active`, `estimated_due`, `awaiting_off_confirmation`, `occupied_pending` and `unverified`. A displayed **zero** is elapsed estimated time, **not** proof of OFF and certainly not proof the backend changed its raw scene to occupied.
- Keep `sleep_overdue` as the **still-reported-ON after expected power-OFF deadline** anomaly with the existing additional five-minute diagnostic grace and fresh ON corroboration. This is not an anomaly merely because raw `scenary=sleep` remains after OFF. No automatic P1 OFF or forced Home while ON.
- Diagnostics and attributes must not expose identifiers, MAC, PIN, credentials or coordinates. No history spam or per-minute writes into session storage.

### 7B. Persisting an HA-owned session

- Persist an **HA-initiated, acknowledged Sleep selection** in a private per-entry/per-device record using Home Assistant `homeassistant.helpers.storage.Store` under local `.storage`. Fields: UTC time of the final accepted Sleep PUT, last verified `sleep_time`, schema version, device/entry scope, current session generation and confidence/provenance. Do not put it in `entry.options` or in the Git repo. Never store access tokens there.
- Save only on meaningful transitions (new Sleep, change of scene, invalidated timer), not on each minute of countdown. If storage fails, the live session may remain active but its restart persistence must not be claimed.
- On reload, load record **before scheduling automatic expiry cleanup** and reconcile against newly received backend scene, power and `sleep_time`. Confirmed non-Sleep invalidates it; a mismatch, corrupt record or clock anomaly invalidates clock precision. Do not reuse OFF confirmations from before restart as if fresh.
- A stored deadline is useful for the estimated timer UI. It is not cryptographic evidence that no other client reselected Sleep while HA was offline. If such a same-scene rearm is possible and cannot be excluded, downgrade the recovered clock's confidence; consider the conservative upper-bound path below for automatic OFF-only cleanup.

### 7C. Startup already in Sleep, but no trustworthy start time

- **Do not assume boot time is the real Sleep start.** Use the timestamp of the **first newly completed valid backend observation** after startup (which may be later than HA boot), plus the current valid `sleep_time`, as a deliberately conservative *latest plausible deadline* for Sleep already observed. The device could really have been counting down before HA boot.
- If scene remains Sleep, never ON was observed, duration is unchanged and power remains OFF, wait until this **upper bound plus the proposed 30-second settling margin**, then require two *distinct fresh OFF observations after that bound* via PUSH, GET or a mix. Only then allow one non-retried scene-only PUT to occupied under current session/intent/lock checks. Never send P1.
- If an ON→OFF transition is genuinely observed while Sleep is active **before the conservative bound**, it **does not** cancel Sleep or allow early occupied cleanup. A later ON (including from HA) may continue the same countdown; do not insert automatic occupied-before-ON solely because the exact start is unknown. Wait for the bound+settle and two fresh post-bound OFF reports if the unit is OFF. If Sleep remains ON beyond the deadline, report only read-only overdue after its own guard; no forced power control.
- If a new manual Sleep/scene change or timer-duration edit occurs, re-evaluate/discard old deadlines. Disconnects, stale snapshots, unverified scene writes and competing client activity require a conservative stop, not an automatic cleanup.
- **Fundamental limitation:** no local store or GET snapshot can prove whether a different app rearmed the *same* scene invisibly while HA was offline. In installations where silent external rearm must never be interrupted, unknown-session automatic cleanup must remain conservative/disabled until verifiable new evidence or manual Home. Present a startup-based deadline as an upper-bound estimate, never as exact backend time remaining.
- Example: true Sleep start 22:30, duration 30 min, HA first reliably sees Sleep at 22:45 but has no trustworthy stored start. Estimated true expiry might be 23:00; conservative upper-bound cleanup is not before 23:15 plus settling and two OFF samples. If a durable 22:30 record is verifiable, the UI may show 23:00 as its expected end; do not blindly claim session identity in the presence of possible offline rearm.

### 7D. Regression and UX acceptance

- Simulate restarting mid-Sleep with matching/different/missing/corrupt stored record; asynchronous storage failure; altered system clock; no internet until after HA boot; scene/duration change; new entry/device; and leaked or stale events.
- Test full B-revised sequence with valid HA cleanup setting ON: Sleep t0, physical OFF at t+30, P1 ON t+240, original backend OFF near t+300, **no automatic occupied before t+300+settle**, cleanup after two independent current OFF observations. A proper OFF+ON does not reset stored t0 or remaining-time diagnostics.
- Test C sequence: Sleep t0, remote P1 OFF t+120, stays OFF to deadline; no occupied before deadline, one occupied after expiry+settle+two OFF observations. Test delayed GET/PUSH and duplication rejection.
- Repeat Sleep while **active**, ON or OFF, before expiry: **zero network writes**, no new countdown, no session revision/Store write, original deadline unchanged. Specifically test `OFF → Sleep → select Sleep again at t+2m → ON at t+4m`: no implicit rearm and original expiry estimate remains t+5m.
- Repeat Sleep while raw Sleep remains after **verified completion**: only an intentional request for a new Sleep session may do occupied→confirmed→sleep, retaining power; rejected/ambiguous first or final PUT never claims rearm. Unknown/ambiguous completion: no implicit rearm; manual Home then Sleep remains available.
- Remaining-time sensor updates without API/INFO traffic, displays zero without falsely publishing OFF/occupied, correctly shows unknown and conservative upper-bound provenance.
- Unknown startup-bound session must not clean early even if panel/remote ON→OFF occurs; two post-bound-and-settling independent OFF samples and no newer ON allow one scene-only occupied. A panel OFF before the bound is never in itself a cancellation; an HA ON from OFF with unknown timer must not send occupied, since it might cancel an active backend timer.
- Source-specific OFF evidence requirements from §2C continue to hold across restarts: two distinct observations not merely one cached snapshot repeated.

**Implementation gate:** this approved policy governs the implementation and
regression coverage on PR #234. Keep #234 unmerged until CI, review comments,
and the full Climate/Switch/Coordinator/SyncManager/Store audit are verified.
The unattended tests are synthetic; they do not extend the observed physical
evidence. Test F remains unauthorized.
