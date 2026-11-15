# Proposed supervised real setpoint trial (#197)

Status: **design only; no real session authorized or enabled**. Baseline:
`635720037b7a2a17debb57e2497ed0b6e359156d`. This proposal does not modify
the integration, existing LAB policy, endpoints, network filters or launchers.
`REAL_DESTINATIONS_ENABLED` remains `False`. PR #222 is outside this change.

## Feasibility and evidence

The existing LAB can be reused, but this experiment is **not an existing safe
configuration option**. It needs a separately reviewed, scoped admission path.

| Component | Existing behavior | Reuse or required change |
| --- | --- | --- |
| `isolated_ha_process.py` | Real independent HA process and disposable config | Reuse process ownership and initialization |
| `isolated_ha_lab/` | Synthetic HTTP/WS backend, multiple installations and entity controls rejected | Keep this baseline synthetic and read-only |
| `protected_ha_lab/` | Authentic integration, login, entry/platform setup, SyncManager and observation hooks | Reuse setup and finalization; add a separate supervised scenario |
| `protected_lab.py` | Sole gateway, process audit hook, session/transport enforcement, quotas, deadline and security stops | Reuse enforcement; do not relax it or flip the global real flag |
| `lab_policy.py` | Exact REST/WSS destinations, headers, bodies and scoped subscriptions; only INFO is allowed on `/events` | Preserve `ReadOnlyPolicy`; a separate policy must admit only the two planned P7/P8 requests |
| `lab_tls_backend.py` | Loopback resolver and disposable HTTPS/WSS trust; controls counted and rejected with 405 | Add opt-in synthetic setpoint outcomes for the new scenario; default remains rejecting |
| `lab_lifecycle.py` | Cancels/awaits owned work and attempts all cleanup steps | Reuse, including pending-I/O checks before transport closure |

Historical private reports document an authenticated real listener with one
subscription ACK, 17 received/applied device events, 19 accepted INFO requests
and one applied recovery GET. The correlated setpoint publication was 0.7911 s
after the last accepted INFO. Exact command time was unknown, the owner also
selected heat, and the run stopped upon correlation. It does not establish an
isolated P7/P8 round trip, two post-command INFO cycles or confirmed restoration.
The approximately 5 ms coordinator-to-entity result measures local processing.
The later approximately 19 s marker reuse is not a new delivery measurement.

The historical panel run demonstrated external power delivery through Pusher,
not P7/P8. TLS, ACK and real events were observed historically; current synthetic
CI does not reauthenticate the current integration against DKN or prove physical
application. Previous session permissions are consumed and cannot be reused.

Importantly, an archived private launcher rewrote the real-destination flag in
its temporary copy. **That launcher cannot be reused under the current rule
that the flag remain false.** Reuse its instrumentation ideas and evidence,
not its activation technique. The current public launcher fails before reading
credentials when `--protected-real` is selected. Supplying credentials or
setting that CLI option does not provide the proposed capability.

## Smallest proposed extension

Keep the current read-only and synthetic launch paths unchanged. Add a separate
session-capability factory and scenario to the same LAB, using the same gateway,
audited session, TLS validators, HA process and teardown. No alternate socket,
raw HTTP client, generic writable mode, environment-only enable flag or source
rewriting is acceptable. The new factory would be an explicit reviewed entry
point, not an exception silently added to `real_policy()`.

Keep TLS at least 1.2 with CA/hostname verification and the existing
service-scoped WSS compatibility policy. Current `info.md` documents that the
static-RSA suite is enabled for that origin and lacks forward secrecy; this
proposal neither changes that policy nor reuses an old session's TLS permission.

The capability must be checked before credentials are read or any connection is
constructed. Bind it to the reviewed source digest, a unique consumed session
nonce, exactly one installation and device, a monotonic deadline and fixed
quotas. Store actual identifiers only in private scope data. An atomic exclusive
session journal prevents restarting the same grant after cancellation/crash.
Flush and fsync admission records before delegation; journal failure denies the
attempt. Concurrent processes must share the exclusive session claim.
Do not put permission or credentials in argv, public output or HA storage.

Proposed limits fit the existing supervisor rather than increasing its ceiling:

- At most **300 s parent process**, **285 s active session** including startup;
  the remainder is for cleanup. Start restoration by active second 225, or
  earlier when either of two required INFO cycles has unusually long delays.
- At most **40 state attempts**, including both controls and all discovery,
  INFO and GET requests; existing spacing of at least 10 s remains enforced.
- At most **two authentication attempts combined**: one login and one private
  authorization. One socket connection, one private subscription, no reconnect.
- At most **two control transport attempts**, never refunded after failure.
  A local readiness failure before admission consumes no control attempt.

If startup, cooldown or latency leaves insufficient time/quota for both
observation and restoration, abort before the first control. Deadline closure
must never issue an automatic restoration command.

Credentials are supplied through an inherited private pipe after approval, used
in memory and removed from temporary HA configuration on exit. Login responses
and tokens must not enter the journal. A password already stored privately is
not an ephemeral credential: reading it requires the new approved scope; this
experiment should not create another persisted copy. Memory disposal does not
claim secure erasure of immutable Python strings or server-side token revocation.

## Admission state machine

1. **Prepare/read only.** Require current subscription ACK, at least one valid
   scoped device event, completed subscription resync and a recent backend
   snapshot. Verify raw and effective Home, power ON and mode exactly COOL (1)
   or HEAT (2). Record original whole-degree setpoint and actual min/max fields
   for that mode. Missing, contradictory or nonintegral values cancel the trial;
   do not substitute Climate's fallback limits. Record the installed source SHA,
   ordinary INFO timing and any pending GET/recovery/backoff.
   Verify device membership in the authorized installation. If discovery would
   initialize any other installation/device, fail closed; do not broaden the
   grant to match everything returned by the account.
2. **Freeze plan.** Choose original +1 if valid, otherwise original -1, otherwise
   cancel. Freeze channel P7/`cold_consign` or P8/`heat_consign`, original,
   target and limits. The owner reviews the two exact numeric operations and
   explicitly arms this plan before any control. A changed mode, power, scene,
   limits or competing setpoint revokes it; never automatically recompute it.
3. **First attempt.** Invoke the authentic HA Climate set-temperature service.
   At the actual transport boundary, under a single admission lock, recheck the
   capability, current preconditions and exact canonical JSON body. Persist and
   consume slot 1 before delegating. Permit only `POST /events` with exactly
   `event={cgi:modmaquina,device_id:<scoped>,option:<frozen>,value:<target>.0}`.
   Snapshot/copy the validated request so mutation during an awaited budget
   admission cannot change the delegated body. Revalidate after budget waits.
4. **Observe.** A 2xx means accepted, not confirmed. Leave ordinary INFO/GET
   scheduling intact; inject no extra reads during the observation window.
   Wait for at least two ordinary INFO cycles and record matching, missing and
   conflicting Pusher fields. If no confirming telemetry arrives, stop and
   consult the owner rather than assuming that the modified value is applied.
5. **Restore.** After successful, confirmed first change and owner go-ahead,
   consume slot 2 for exactly the original target via the same authentic
   Climate service. Require unchanged ON/Home/mode/limits and the expected
   modified setpoint corroborated by received backend telemetry, not only an
   optimistic HA overlay. Observe restoration, then permit a separately marked
   verification GET after the measured window if no ordinary snapshot suffices.
   If the GET is cached, report that limitation; it is not physical proof.
6. **Finish.** No further controls, even if either attempt failed or was
   cancelled. Require snapshot restoration confirmation for a successful result.
   Cancel/join scenario and SyncManager tasks before closing transport; remove
   temporary HA storage/config, join the child, and retain only sanitized results
   and the private recovery journal. Confirm no observer-owned survivors.

The separate policy must reuse existing read/subscription rules and unchanged
header, origin, redirect and TLS guards. For control admission it must reject
every other method/path/body, device, installation, P-code and target, including
P1/P2/P3/P4, all PUTs, Sleep cleanup and automatic Away exit. State validation
alone is insufficient: the transport boundary must reject incidental commands
even if an authentic entity or background cleanup task produces them.

The authentic API already avoids replaying device writes after timeout/5xx,
but `send_event()` can retry a short HTTP 429 cooldown. The existing supervisor
stops on 429; retain that stop and test it through the authentic API so no
second HTTP attempt can escape after the first slot was consumed. Count actual
transport attempts, not HA service invocations, and do not assume that calling
the service exactly twice is sufficient to enforce the wire limit.

After **any ambiguous write, disconnection or unexpected state**, revoke further
automatic controls and stop the experiment. A timeout or cancellation consumes
its slot before delivery can become uncertain. Do not replay it, and do not
automatically use slot 2 as a retry or cleanup. Preserve original/target, scoped
identity, attempted slots, outcome and latest observed state in a private 0600
recovery journal. Ask the owner to verify and decide on restoration through
normal controls; a new operation needs explicit supervision and cannot reuse a
consumed session grant. Restoration cannot be guaranteed after a network failure.

## Measurement without changing the sequence

All child observations use `time.monotonic_ns()` with one recorded origin. A
parent/chat prompt time is not the action timestamp. Capture command invocation,
actual HTTP dispatch and response separately; distinguish queued time from wire
latency. Record INFO dispatch and response independently because a device frame
can arrive while INFO HTTP is still pending.

Extend the existing private-style observation hooks, without changing production
integration bytes. Capture scoped decoded Pusher `device` reception before merge,
exact consign field/value, coordinator publication and HA state publication.
Label command overlay publication as optimistic; never infer Pusher provenance
from a matching entity value. Record GET dispatch/response and fields actually
applied after revision fencing separately. An event can precede HTTP ACK; do not
discard such events or attribute an intervening GET to Pusher.

Only allowlisted enum/temperature/status/timing fields enter public results.
Never store complete URLs, auth bodies, channel names, raw frames or payloads.
Use Device A publicly. Preserve chronological order rather than just last-event
ages or aggregate counts. The current hooks alone are not the complete recorder.

Report, separately for change and restoration: matching field received or not
received within the bounded window; preceding and following INFO dispatch and
response; elapsed command-to-event if captured; GET interference; optimistic and
confirmed HA publication; final snapshot confirmation. A frame after INFO does
not prove INFO caused it. A frame before this LAB's next INFO does not exclude
INFO from another HA/app session; report other clients as a confounder rather
than disconnecting them. Do not alter production scheduling to create causality.

## Required proof before a real launch exists

The accompanying tests exercise the **current** unchanged deny boundary with
canonical P7/P8 and other real command forms. They prove rejection before the
underlying session is called, not safety of an unimplemented writable capability.
Existing loopback TLS, redirect, alternate-session, quota, cancellation and
independent-HA tests remain applicable.

The extension must not be launchable until these additional tests pass:

| Layer | Required assertion |
| --- | --- |
| Grant | Missing/expired/wrong-scope grant, reused nonce, concurrent launch or wrong source digest fails before credential read or network |
| Plan | COOL/P7 and HEAT/P8; valid +1, upper-bound -1; missing limits, invalid original, OFF/Away/Sleep/aliases abort with zero controls |
| Admission | Only frozen target then original are accepted; duplicate first, premature restore, changed channel/device/limits, third attempt and every other control leave backend count unchanged |
| Races | Two simultaneous service calls, mutation during budget wait, mode/scene changes while waiting and duplicate context entry cannot consume extra slots or change the wire payload |
| Outcomes | Timeout/cancel before ACK, acceptance then timeout, 429 retry path, response failure, disconnect, crash and restart never refund/replay; no optimistic success on failed command; recovery journal survives |
| Authentic HA | Actual service → Climate → API → guarded transport sends exactly two planned requests; automatic Away/Sleep/power paths cannot escape; integration source digests unchanged |
| Recorder | Synthetic push-before-ACK, before-INFO, after-INFO, GET-only and no-event cases retain correct source, fields and monotonic order; observation adds no INFO/GET |
| TLS/lifecycle | Same endpoint/verified TLS/redirect/audit rules, one connection; pending auth/GET/control joined before closing; zero credentials in artifacts/storage; hard deadline leaves no owned processes |

Use the existing synthetic backend with opt-in responses for these tests;
synthetic timing is not physical evidence. A finite test matrix demonstrates
specified cases, not that arbitrary future code cannot evade process-local
guards. The container remains the security boundary.

## Future approval wording, after implementation and review

Do not request this approval for the proposal alone. First present an exact
reviewed commit/digest and a completed synthetic acceptance report. Then ask:

> I authorize one supervised LAB session of the reviewed build for Device A in
> the privately selected installation, up to 300 s including cleanup, one login,
> one private authorization, one socket and no reconnection, at most 40 state
> attempts and two P7 or P8 control attempts. The controls may only change the
> confirmed original setpoint by exactly 1 °C within actual limits and restore
> that original value. Show me the frozen numeric plan before sending it. Use
> credentials in memory, keep certificate/hostname verification and the current
> service-scoped TLS policy, and leave the existing global real flag false.
> Do not change power, mode, scenes, Sleep or any other setting. Stop on ambiguity
> and consult me; no automatic retry or restoration after an ambiguous outcome.

P7/P8 results cannot validate scene PUT delivery. Optimizing #222 recovery would
need separate evidence for ordinary scene PUT acceptance versus inbound scene
patches and missed/delayed delivery under ambiguous outcomes. Keep its synthetic
timeout/cancellation/no-replay regressions; do not provoke real scene errors or
merge/modify that PR on the strength of this experiment.
