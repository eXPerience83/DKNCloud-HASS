# Releasing DKN Cloud for Home Assistant

## Model and authoritative version

**Draft first; publish manually.** Run the [Release workflow](./.github/workflows/release.yml)
to prepare a validated ZIP in a draft release. Publication is never automatic.

The integration version belongs to
`custom_components/airzoneclouddaikin/manifest.json`, **not**
`pyproject.toml`, whose version tracks independent development/test tooling.
The first `CHANGELOG.md` version heading must match the integration version.
The tag is derived as `v<version>`: `0.5.0a6` maps to `v0.5.0a6`.
Allowed formats are `X.Y.Z` (stable), `X.Y.ZaN` (alpha), `X.Y.ZbN`
(beta) and `X.Y.ZrcN` (release candidate). Alpha, beta and RC must be
marked as **Pre-release**.

The published stable `v0.4.8` and alphas `v0.5.0a4`/`v0.5.0a5` remain
immutable. Never reuse their tags, edit published ZIP assets, or move tags.

## Step 1 — Prepare the source

1. Create a PR containing the next integration version in the manifest and a
   matching first version heading in `CHANGELOG.md`. Follow `AGENTS.md`'s
   changelog rules and include migration/backup and HA support warnings.
2. Update relevant user-facing docs. Do **not** bump `pyproject.toml` or
   regenerate `uv.lock` just to match the manifest version.
3. Confirm all required CI checks, review comments and code changes. For
   normal stable releases, **Squash and merge** the PR into `main`.
4. For intentionally unmerged alpha/beta candidates, a reviewed branch from
   the same repository is permitted, but verify the selected commit carefully.

The workflow uses the selected snapshot's pinned Python 3.14, `pyproject.toml`,
`uv.lock`, full pytest suite, Ruff, and DKN Marketplace ZIP validator. It is
not intended to rebuild old historical DKN snapshots with obsolete tooling.

## Dependency cooldown and exceptional prerelease validation

The default uv cutoff is `exclude-newer = "3 days"`. For a new supported
Home Assistant/official pytest test pair, a maintainer may run the reviewed
baseline updater on `main` with `override_cooldown=true` and a printable
15–200 character reason. This only waives maturity for the exact compatible
pair and writes **fixed UTC per-package cutoffs**, bounded to its reviewed PyPI
artifacts, into the resulting PR's `pyproject.toml` and `uv.lock`. All
transitive packages retain 72-hour maturity; all lock, test, coverage, lint,
package and PR review requirements remain in force. There is no global
`UV_EXCLUDE_NEWER=false` override or release-specific bypass.

The initial 2026.11.0 scoped exception expired on **2026-11-08 13:03:59
UTC**. Its cutoffs were removed by [#184](https://github.com/eXPerience83/DKNCloud-HASS/pull/184),
with all package versions and hashes preserved, and stale comments removed
by [#185](https://github.com/eXPerience83/DKNCloud-HASS/pull/185).
The ordinary `exclude-newer = "3 days"` policy now applies to all packages.
For future temporary exceptions, the reviewed updater can propose a
normalization-only PR after maturity even while another candidate cools;
no update or cleanup PR may merge automatically.

Release checks ensure that `pyproject.toml` and `uv.lock` agree on the
cutoffs and reject preparing a stable version while a scoped exception
remains. Prereleases continue using the existing draft-first checks; approval
and publication remain manual, with no changes to the published ZIPs.

## Step 2 — Prepare a draft in GitHub Actions

1. Open **Actions → Release → Run workflow**.
2. Select **main** as the workflow branch.
3. For a validation-only rehearsal, set **`dry_run=true`**. This executes
   the validation/build/digest steps but **skips tag and draft creation**.
   After reviewing the workflow artifact, run again with `dry_run=false`
   to prepare the draft.
4. Leave `release_ref` blank to select the current `main` commit, or enter
   the exact workflow main SHA. Other same-repository branches, tags and full
   40-character SHAs require `dry_run=true`. Forks,
   pull-request pseudo-refs, abbreviated SHA and Git revision expressions
   are rejected. A selected SHA must be reachable via a repository branch/tag.
5. Run it. The **read-only validation job** selects an immutable commit,
   checks metadata, locks and tests, builds `airzoneclouddaikin.zip`, checks
   its installable ZIP-root layout and calculates its SHA-256.
6. Unless `dry_run=true`, the separate **write-scoped finalization job** checks the artifact digest,
   existing tag target and release state. If safe, it creates or reuses the
   matching tag and creates a draft or reuses its identical ZIP.

Do not run preparation against published `v0.5.0a5` on current `main`:
it will refuse to modify that release. Create a new version first.

## Step 3 — Review and manually publish

- [ ] Verify selected SHA, tag, prerelease classification and ZIP SHA-256
      in the workflow summary.
- [ ] Confirm the tag points to the validated SHA and the draft contains
      exactly the expected `airzoneclouddaikin.zip` asset.
- [ ] Check release notes, backup/rollback guidance, HA minimum, migrations,
      and device compatibility; edit the draft notes if necessary.
- [ ] Confirm **Pre-release** for `aN`/`bN`/`rcN`; disable for `X.Y.Z`.
      Prereleases must not be marked GitHub's latest stable release.
- [ ] Click **Publish release** only after the above review.

When the published event fires, the workflow finds the already attached ZIP
and exits without modifying that binary.

## Step 4 — Verify the published release

Check the GitHub release ZIP; test installation or Redownload from HACS /
Home Assistant Marketplace, restart Home Assistant, confirm integration
version and perform smoke tests on a real DKN Cloud device. Passing CI alone
does not establish real-device functionality.

## Recovery and reruns

An unpublished draft's ZIP is reused only when its SHA-256 matches. Missing
assets may be added; different or unverifiable existing assets fail closed.
Manually edited notes are preserved. If draft creation fails after tag creation,
rerun the same validated main snapshot; never move or force-update the tag.

The `release: published` handler is an observer. A missing ZIP fails and requires
maintainer investigation; it does not upload or repair published artifacts.

To recover from a defective published version, prepare a **new version**.
Do not silently overwrite its published artifacts or roll back a tag.

## Release checklist

- [ ] New manifest version and latest changelog section match.
- [ ] Exact source ref/SHA and CI are verified.
- [ ] ZIP validation, expected filename and SHA-256 are verified.
- [ ] Draft notes and alpha/beta/RC or stable settings are reviewed.
- [ ] Publish is performed manually after ZIP attachment.
- [ ] HACS/Marketplace installation and hardware smoke test pass.

### Updater pull-request checks

Updater PRs use `GITHUB_TOKEN`, which does not trigger their normal PR workflows.
A repository writer must close and reopen the PR to trigger required CI, then
review the exact checked HEAD before merging. Never bypass required checks.
Manual waiver reasons are preserved literally in the review artifact.

## Reviewed main preparation and immutable retries

Dispatch Release from `main`. A non-dry run must select the exact main commit
that supplied the workflow. Other branch, tag and historical references are
validation-only (`dry_run=true`). A future beta can therefore be rehearsed by
exact SHA without creating a tag, draft or published asset. Merge its reviewed
PR and wait for main CI before preparing its draft.

Immediately before writes, trusted workflow code checks that main has not moved
and that the latest GitHub Actions results for all eight required jobs on that
exact SHA are successful. Missing, pending and failed reruns fail closed.
Release uses no dependency cache. Publication remains a separate manual step;
wait for the entire workflow to finish before publishing or editing the draft.

ZIP timestamps come from the selected commit. An existing draft ZIP is reused
only when GitHub reports the same SHA-256; an unverifiable or different asset
is never overwritten. Published events only check asset presence and cannot
repair or replace assets. Older tags retain their historical workflow code;
this policy does not retroactively attest those releases.

Before reporting success, finalization rereads the remote draft and all assets
and requires the validated SHA-256 and prerelease classification. A concurrent
upload skipped by the action cannot masquerade as a successful validated ZIP.
Lint always checks the triggering SHA; manual runs select their workflow branch
without a separate checkout override that could mislabel another commit's check.
