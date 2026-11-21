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

**Rule:** Sleep is a backend-controlled countdown initiated by a **new accepted selection** of `scenary=sleep`. HA must not implement another shutdown timer, schedule P1 OFF by wall clock or silently rearm Sleep on power toggles. The backend may retain raw `scenary=sleep` even after the timer or a manual OFF. When enabled, HA restores scene to `occupied` **without changing power** under the guarded cases below.

### 2A. Selecting Sleep

- When OFF + occupied: a user's Sleep selection writes Sleep; **no P1 ON**, and OFF observations belonging to the initial OFF state are **not** mistaken for a newly completed ON→OFF transition.
- When ON + occupied: select Sleep; retain ON.
- Keep the server-accepted Sleep selection timestamp, expected `sleep_time`, scene revision and original ownership for the current session. A local command acknowledged by the backend and an externally discovered scene are **not equally precise timing evidence**. Do not mark the timer start as known merely because HA first saw Sleep in a delayed PUSH/GET after startup.
- Mode/fan/setpoint edits during active Sleep preserve its scene. Changing Sleep duration during a running session invalidates calendar-based expiry inference; do not assume the backend rescheduled it.
- A new *explicit* rearm after an expired or canceled session that still reports raw Sleep uses serialized `occupied → sleep`, with verified ordering and no automatic ambiguous retry. No automatic rearm due to P1 ON.

### 2B. ON while Sleep is still active

- If raw/guarded power is already confirmed ON, **do not send a redundant P1 or cancel Sleep**.
- If power is OFF because Sleep was selected while OFF and a trustworthy known countdown has **not** elapsed, an explicit HA ON sends only the necessary P1 ON, **retaining Sleep and the original deadline**; matches real trial A.
- If an earlier ON→OFF **has been corroborated as a completed/canceled Sleep session**, or a known Sleep deadline elapsed and the unit remains confirmed OFF, new HA ON first ensures occupied and then sends the necessary P1 ON. Do not expose the next ON to an old countdown merely because occupied cleanup is still queued.
- If the current Sleep start/deadline is unknown (e.g. HA restart, delayed external scene), an explicit HA wake from confirmed OFF uses a conservative occupied-before-ON policy to avoid an unknown residual timer. This is a client safety decision, not proven backend behavior.
- A newly selected Sleep or a newer power/scene intent supersedes earlier scheduled work. Do not resurrect expired Sleep or overwrite a newer manual selection.

### 2C. OFF during an ON-observed Sleep session

- Whether natural expiry, panel OFF or HA/remote P1 OFF, a **corroborated OFF belonging to the current Sleep session** is eligible for cleanup to occupied **as soon as it is confirmed**; no wait for the original timer's deadline.
- A successful HTTP P1 OFF ACK does not itself prove physical OFF. A failed/ambiguous P1 response does not justify assuming OFF or issuing automatic replay.
- Confirm OFF from **two independent newly applied power observations** after the current session's ON, from PUSH **or** GET, or a mix. **Do not require one from each transport.** Two distinct backend GET transactions may count; two independent PUSH deliveries may count. A duplicate event, replay of the same payload, retained state on a maintenance tick, stale pre-write GET or the same in-flight GET surfaced through multiple paths counts **once** at most.
- First real OFF report requests bounded recovery. Confirm second observation is distinct, current, not overturned by newer ON and associated with the same session. Respect the existing minimum spacing/budget/backoff; never force excessive polling.
- After confirmed OFF, send at most one non-retried scene PUT `occupied`, keep P1 OFF, and request scene readback. Ensure a later ON explicit intent that arrives before cleanup does not get sent under the expired/canceled Sleep scene. If unable to safely complete the scene transition, surface uncertainty rather than silently pretending cleanup succeeded.

### 2D. Sleep started OFF and never observed ON — **new required path**

**This path MUST be supported even though the current code requires `observed_on=True`.** The clock can run without the device being ON (supported by real trial A). The lack of ON is not a reason to leave raw Sleep indefinitely.

- **Never clean immediately** from baseline OFF or from two OFF observations before the known timer deadline: doing so would cancel a valid Sleep selected while OFF.
- For a **locally acknowledged Sleep selection with known clock and verified unchanged duration**, let `T = accepted Sleep time + sleep_time minutes`. After `T` **plus a small conservative settling window (proposed 30 seconds; implementation should test this as a policy constant, not conflate it with the existing five-minute read-only ON-overdue margin)**, request bounded fresh observation.
- Require two independent **post-deadline** OFF reports (PUSH and/or GET, as in 2C), the same current Sleep session and raw scene Sleep, no newer ON or scene selection, and a valid active entry. The 30 s window and corroboration make this a deliberately conservative **HA cleanup policy**, not physical proof that the backend has consumed its timer while always OFF.
- After confirmation send only `scenary=occupied`, no P1 OFF or ON; one attempt, no ambiguous replay, verify scene via readback. A second GET may be scheduled by the existing budgeted recovery if no fresh reports arrive. If they do not arrive, leave Sleep pending instead of creating false evidence.
- If timer start is unknown, duration changed, backend unavailable or state uncertain, **do not perform automatic time-based cleanup**. Keep reported Sleep; manual Home is still allowed. An explicit HA ON from an OFF unit may instead perform the conservative occupied-before-ON behavior in 2B.
- If any distinct fresh ON appears before cleanup, invalidate the OFF-only candidate. If ON arrives after the expected deadline and remains ON, retain Sleep and surface only the existing read-only overdue anomaly after its own confirmation; do not auto-P1 OFF.

### 2E. Option and manual control

- Keep `sleep_cleanup_enabled`, **default ON**. Enabled = automatically write occupied only on eligible confirmed OFF or known elapsed OFF-only session. Disabled = **no automatic occupied write**; expose raw Sleep faithfully and allow manual Home, Away or explicit Sleep rearm.
- Label/description may be clarified to **"Restablecer Home al apagarse o finalizar Sleep"**, without changing the persisted option key.
- Manual Home/Away always outranks delayed cleanup. Never mark Home as verified just because HTTP returned 2xx.
- An explicit HA OFF when already OFF is normally P1-idempotent; it need not manufacture a new ON→OFF transition for the OFF-only case. The user's intentional choice of Home is a separate scene command.

## 3. Safety and race contract

- **No automatic P1 replay**, no automatic retry of ambiguous scene PUT, no 3xx success/redirect replay, no loss of already accepted P1 after caller cancellation. These are the other currently open #234 CodeRabbit findings and must be implemented/audited independently within #234.
- Serialize per device using existing entry ownership, scene revision, preset intent, power-intent generation, locks and pending readback. Do not add a competing global command queue or a second timer that turns power off.
- Do not assume two samples are independent merely because they arrived on different transports; require separate eligible source observations. Do not require PUSH specifically: OFF **PUSH or GET** is sufficient if independently corroborated.
- Refuse stale writes after unload/reload, when a newer Sleep/occupied/away/ON request supersedes, or when a scene exit is unverified. An accepted HTTP scene PUT is `pending` until fresh scene evidence confirms actual occupied.
- An ambiguity should keep the correct pending state, request bounded readback and never silently continue to P1 ON as though the scene had changed. No repeated physical commands after unknown dispatch.
- Avoid using the ordinary ON-overdue diagnostic's `SLEEP_TIMEOUT_GRACE_MINUTES=5` to delay OFF-only cleanup: the diagnostic is read-only and serves a different purpose.
- After HA restart, persisted Power/scene telemetry alone cannot reconstruct an accurate t0 for a pre-existing Sleep session; do not invent deadlines.

## 4. Required changes for #234 (documentation now; code later)

1. **Sleep session derivation** in `__init__.py`: retain a reliable ACK-based local start for the no-ON path. Distinguish ON→OFF corroboration from expiry-while-always-OFF. Do not let merely first-seen external Sleep create a "known" exact deadline. Gate later repeated OFF observations and post-deadline recovery correctly.
2. **Candidate scheduler and guard** in `__init__.py` and `helpers.async_auto_exit_sleep_if_needed()`: accept either of the two guarded cleanup reasons; remove the unconditional `tracking.observed_on` requirement from the **OFF-only expired** reason only. Preserve 2 independent OFF evidence. Extend pending/retry tracking without dropping newer manual scene requests.
3. **Climate/Switch ON**: distinguish active Sleep selected while OFF (retain scene) from expired/canceled Sleep (occupied before ON). Share that policy across native Climate, direct Switch and delegated Switch→Climate; do not send redundant P1 when ON is reliably confirmed.
4. **Power OFF**: keep idempotent and distinguish ACK, uncertainty and confirmed physical/backend state. When ON→OFF confirmed, schedule scene-only cleanup, including physical-panel OFF. No automatic P1 off on timer deadline.
5. **Readback, retries and no-replay**: handle 3xx, 2xx cancellation, accepted vs not-dispatched and ambiguous scene writes, along with unload and newer intents. Avoid forcing a GET if a valid distinct PUSH corroboration already supplies evidence, subject to existing strict recovery budget/field precedence.
6. **Options and documentation**: preserve `sleep_cleanup_enabled` and its default; update `info.md`, translations and tests **when implementation lands**, so documentation reflects actual runtime rather than only this target design.

## 5. Regression matrix before merging #234

- OFF/occupied→Sleep with 0, 1 or 2 fresh OFF before deadline: **no occupied**, no P1.
- Same, two distinct OFF observations **after T+settling window**: one occupied, no P1, verified GET; test PUSH+PUSH, GET+GET, PUSH+GET and duplicate suppression.
- OFF-only with unknown t0, HA reload, changed duration, stale GET, duplicate PUSH, unavailable/auth failure or missed deadline evidence: **no premature occupied**, no replay.
- OFF→Sleep→P1 ON before deadline: retain Sleep and original countdown; no duplicate P1; at expiry ON→OFF evidence triggers cleanup.
- ON→Sleep→panel OFF confirmed: one occupied, no P1; ON→Sleep→remote OFF confirmed: same; natural OFF: same. Test pair independence and sequence revision.
- Successful 2xx OFF without OFF observations: **no cleanup yet**; ambiguous OFF without corroboration: no replay and no false occupancy.
- OFF confirmed, then a new ON during pending scene cleanup: do not send ON under old expired Sleep; preserve newest intent and report any scene uncertainty.
- New explicit Sleep while cleanup waits for lock, manual occupied/away and reload/unload: older cleanup makes **zero writes** to newer session/entry.
- Sleep cleanup option disabled: no automatic occupied in either path; explicit Home/rearm still work.
- Sleep still ON past estimated deadline: **no forced OFF or occupied**, read-only overdue diagnostic remains.
- Test timer-spaced observation for no-ON path at 5min, with test clock/monotonic fake; do not sleep for wall-clock minutes.
- Transport 304/307/308, 2xx followed by caller cancellation, stale overlapping ON/OFF and ordinary Climate/Switch delegation retained as separate #234 blockers.

## 6. Review/implementation gate

This is **the intended behavior**, not a claim that the current PR #234 implements it. Current code in `__init__._derive_state` and `helpers.async_auto_exit_sleep_if_needed(reason="expiry")` explicitly requires `tracking.observed_on`, and `climate.async_turn_on` / `switch._fallback_set_power` may prepare occupied from OFF Sleep. Those paths need work; do not merge #234 based only on this document.

The actual physical evidence is preserved in its original lab report on draft #237. Trial F remains **deferred**, no permission for an additional real command is implied. When the code implementation is ready, re-run HA current/minimum, protected LAB smoke, CI and CodeRabbit, then conduct independent whole-series audit of #231–#234 before final closeout of #197.
