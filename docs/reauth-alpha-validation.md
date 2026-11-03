# DKN account reauthentication — alpha test gate (#105)

This document is a **manual acceptance gate** for a proposed alpha after
PR #142 and the pre-alpha hardening changes. Passing the synthetic test suite
does not prove a real DKN server invalidates old tokens after changing a password.

## Before testing

1. Use a backed-up Home Assistant test installation if possible, or back up
   the live HA configuration and identify the tested integration SHA/version.
2. Confirm DKN Cloud's official app works and its backend is healthy.
   Record HA version, integration version, UTC start time, and account scope
   without exposing account/device identifiers.
3. Install the explicitly approved **integration** test alpha (not a Home
   Assistant Core beta). On HA 2026.10.x, open this repository in HACS,
   select the three-dot menu, then Download/Redownload and choose the approved
   release under **Need a different version?** On HA 2026.11 or later, open
   Settings > Marketplace, select this repository, enable its **Pre-release**
   option and install the approved version. HA 2026.11 compatibility still
   requires separate validation. Do **not** delete/re-add the integration:
   that would bypass the reauthentication scenario under investigation.
4. Never attach unredacted diagnostics, URLs with `user_token`, passwords,
   email addresses, MAC/PIN, HAR traces, or raw HTTP request bodies.

## Real password-change scenario (requires separate authorization)

1. Record the current config entry ID indirectly (a local pseudonym), entity
   count and existing nonsecret integration options.
2. Change the DKN password through the official account management UI.
   Do **not** put the password into GitHub, chat, logs, or screenshots.
3. Wait for a genuine DKN 401 on an authenticated Home Assistant read.
   If the old token remains accepted, document *no token invalidation observed*
   rather than claiming this tests reauthentication.
4. Confirm HA opens exactly one reauthentication flow for the existing entry.
   Enter the new password; do not create a second config entry.
5. Check that HA only reports success after the new token is accepted by an
   authenticated installation read.
6. Confirm the existing entry returns to Loaded, with the same entities and
   unchanged nonsecret options. Compare a fresh device state with the official
   app without issuing any real HVAC commands.
7. Reload the entry once and restart the test instance if practical. Verify
   persistent recovery and no duplicated entities.
8. Optionally, and only during an explicitly approved second maintenance window,
   repeat the password change to confirm successive reauthentication works.

## Failure categories

| Observation | Classification |
| --- | --- |
| Login endpoint rejects supplied password (401) | Invalid login credentials |
| Login returns a token but installation GET rejects it (401) | Token rejected; entry unchanged |
| Login/installation GET times out or returns 429/5xx | Cloud unavailable; entry unchanged |
| Reauth succeeds, but subsequent device GET fails | Incomplete recovery; retain issue |
| Entry or entities are replaced/duplicated | Regression; retain issue |
| Old token remains valid after password change | Backend did not force a reauth cycle; inconclusive |
| DKN app itself fails simultaneously | Backend incident; inconclusive |

CI additionally exercises loopback HTTP with two token rotations, automatic HA
config-entry reload, entity-registry preservation and write-only 401 reauth.
Real device/network behavior is outside the CI harness. Keep #105 open until
real observations meet the acceptance criteria.

## Additional recovery observations

Record device-registry identity as well as entity-registry identity locally,
including disabled entities and hidden options. Compare all configured accounts
and installations, ensuring only the targeted account is reauthenticated.
Record whether a concurrent reload interrupted the form; restart the native
reauthentication flow if Home Assistant aborted it during reload.
A successful installations probe establishes token acceptance at that endpoint,
not successful device recovery: wait for a healthy coordinator refresh before
accepting the test. A temporary probe failure does not prove a wrong password.
Verify logs after recovery and shutdown, and record duplicate flows or late
reauthentication prompts after unloading an entry as regressions.
The synthetic regression suite additionally covers a malformed
`installation_relations` list, two simultaneous unauthorized writes, and a
write that completes with 401 after its original entry is unloaded or reloaded.
An invalid relations response must not replace a previously healthy snapshot.
