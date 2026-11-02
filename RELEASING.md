# Releasing DKN Cloud for Home Assistant

## Model and authoritative version

**Draft first; publish manually.** Run the [Release workflow](./.github/workflows/release.yml)
to prepare a validated ZIP in a draft release. Publication is never automatic.

The integration version belongs to
`custom_components/airzoneclouddaikin/manifest.json`, **not**
`pyproject.toml`, whose version tracks independent development/test tooling.
The first `CHANGELOG.md` version heading must match the integration version.
The tag is derived as `v<version>`: `0.5.0a5` maps to `v0.5.0a5`.
Allowed formats are `X.Y.Z` (stable), `X.Y.ZaN` (alpha), `X.Y.ZbN`
(beta) and `X.Y.ZrcN` (release candidate). Alpha, beta and RC must be
marked as **Pre-release**.

The published stable `v0.4.8` and alpha `v0.5.0a4` remain immutable.
Never reuse their tags, edit their published ZIP assets, or move those tags.

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

## Step 2 — Prepare a draft in GitHub Actions

1. Open **Actions → Release → Run workflow**.
2. Select **main** as the workflow branch.
3. Leave `release_ref` blank to select the current `main` commit, or enter
   a same-repository branch, tag or full 40-character commit SHA. Forks,
   pull-request pseudo-refs, abbreviated SHA and Git revision expressions
   are rejected. A selected SHA must be reachable via a repository branch/tag.
4. Run it. The **read-only validation job** selects an immutable commit,
   checks metadata, locks and tests, builds `airzoneclouddaikin.zip`, checks
   its installable ZIP-root layout and calculates its SHA-256.
5. The separate, **write-scoped finalization job** checks the artifact digest,
   existing tag target and release state. If safe, it creates or reuses the
   matching tag and creates/updates **only a draft**.

Do not run preparation against published `v0.5.0a4` on current `main`:
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

## Recovery, reruns and emergency fallback

Preparation is idempotent for an **unpublished draft**: it may replace its
ZIP while preserving manually edited release notes. It refuses an already
published release or a tag pointing to another commit. If draft creation
fails after the tag was created, rerun the same validated commit to recover.
Never move or force-update a published tag.

Emergency path: a maintainer can publish a release manually before adding
the ZIP. The `release: published` handler can validate that tag and attach
**one missing** `airzoneclouddaikin.zip`; it must not replace an already
published ZIP or change release metadata. The release is publicly visible
while this runs, so failure may require immediate maintainer action. Prefer
draft-first for all routine releases.

To recover from a defective published version, prepare a **new version**.
Do not silently overwrite its published artifacts or roll back a tag.

## Release checklist

- [ ] New manifest version and latest changelog section match.
- [ ] Exact source ref/SHA and CI are verified.
- [ ] ZIP validation, expected filename and SHA-256 are verified.
- [ ] Draft notes and alpha/beta/RC or stable settings are reviewed.
- [ ] Publish is performed manually after ZIP attachment.
- [ ] HACS/Marketplace installation and hardware smoke test pass.
