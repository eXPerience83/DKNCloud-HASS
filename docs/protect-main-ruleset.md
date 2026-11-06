# DKN Cloud main branch ruleset parity (Pollen Levels reference)

Reference: [Pollen Levels Protect main](https://github.com/eXPerience83/pollenlevels/rules/20281037), verified 2026-11-06; exact job-contexts confirmed against DKN workflows on this branch.

**Status:** Pollen's repository ruleset is active. At inspection DKN Cloud returned **no repository rulesets** via its GitHub API. This versioned [JSON blueprint](./protect-main-ruleset.json) is **not automatically applied** by storing it in Git; a repository administrator must create/import and activate it using [Settings → Rules → Rulesets](https://github.com/eXPerience83/DKNCloud-HASS/settings/rules).

**Before importing the JSON:** read this document and verify that the eight required GitHub Actions jobs report on DKN. The importable JSON intentionally excludes `code_scanning` while CodeQL is not confirmed working. Once CodeQL reports successfully on DKN PRs, an administrator can add its rule manually with the thresholds below. Never activate a CodeQL requirement before that verification.

Recommended settings aligned to Pollen:
- Target: default branch only (`~DEFAULT_BRANCH`), active, prevent deletion and force pushes, require linear history, require PRs, allow only **squash merge**.
- Required status checks (GitHub Actions, integration ID **15368**, **strict/up-to-date**): `actionlint`, `lint`, `tests`, `validate` (hassfest), `validate-hacs`, `zizmor`, `package`, `minimum-compatibility`.
- Code scanning (optional follow-up, **not in the importable JSON**): after enabling and verifying CodeQL on DKN PRs, add the `code_scanning` rule with tool `CodeQL`, `security_alerts_threshold: high_or_higher` and `alerts_threshold: errors`. Enabling the rule first can block all merges.
- Reference review setting: 0 mandatory approvals, but a PR is required; code-owner review off; `require_extra_approval_for_unattributed_changes` true.
- Bypass: reference has repository administrators (`RepositoryRole` 5) in **pull_request-only** mode. Do not allow direct-push bypass of the core rules. Review which accounts should get the role-based bypass before activation.
- Check both fresh PR runs and a squash-merge dry-run before enabling enforcement; do not set required contexts that never appear.

**Separate concepts:** branch-ruleset bypass controls *how changes enter main*. The Home Assistant baseline updater's `override_cooldown` is a **one-run, reasoned manual waiver only for the HA/pytest publication age check**, not a permission to merge despite failed CI or bypass rulesets. Its `workflow_dispatch` requires `main`, an explicit reason, lock validation, full pytest, coverage and lint. It never merges or publishes automatically.

Not part of this PR's automated mutation: GitHub repository settings and required CodeQL setup. A ruleset must be activated using an administration-capable GitHub account/app. After activation, verify the required check contexts after #170 lands on main.
