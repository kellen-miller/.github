# Shared repository defaults

`renovate-config.json` makes Renovate onboarding suggest the shared preset in
[`kellen-miller/ci`](https://github.com/kellen-miller/ci).

Renovate discovers this file in the public `.github` repository when no
`kellen-miller/renovate-config` default preset exists. Existing repositories keep
using their committed configuration until it is updated explicitly.

The Renovate app must have access to each repository and interactive onboarding
enabled. This file selects the onboarding preset; it does not change app access
or override silent mode. Fork processing follows the app's installation settings.

## Personal repository rulesets

`rulesets/baseline.json` defines three active rulesets targeting exactly `main`:

| Ruleset | Protections | Bypass |
| --- | --- | --- |
| `personal/main` | No deletion; PR required; merge/squash only; CI required against current main; checks exempt on branch creation | `kellen-miller`, PR-only |
| `personal/reviews` | One approval; dismiss stale approvals; approve latest reviewable push; extra approval for unattributed Copilot PRs | `kellen-miller`, exempt; `renovate-approve`, PR-only |
| `personal/force-pushes` | Block force pushes | `kellen-miller`, always |

Conversations need not be resolved. User ID `37915888` and App ID `7394` are
GitHub's IDs for these bypass identities, not the reconciliation App.
Kellen is exempt from the review ruleset, including latest-push approval, so
merging does not require selecting a manual override. GitHub does not record a
bypass audit entry for exempt actors. The separate main ruleset lets only
`kellen-miller` bypass CI when merging a PR, including squash merges while checks
are pending or failing. Use GitHub's merge override (or `gh pr merge --admin
--squash`) when needed. This PR-only bypass still requires a PR; other actors
must pass CI. Force-push permission alone does not bypass the main ruleset.
The Copilot field `require_extra_approval_for_unattributed_changes` is returned
by GitHub's live ruleset API, although absent from its published OpenAPI schema.
The script sends it explicitly and verifies the saved configuration after writes.

Each repository declares its own required checks in
`.github/ruleset-requirements.json` on its default branch:

```json
{
  "checks": ["build", "test"]
}
```

Check names must be nonempty, unique, and free of surrounding whitespace and
control characters. They are bound to the GitHub Actions App (`15368`). Declare
checks that run on every PR; an absent path-filtered check can block merging.
The optional `release_deploy_key_id` must be a positive integer. It permits
deploy-key bypasses in `personal/main` and `personal/reviews`; force-push
protection stays enforced. GitHub represents this bypass with a null actor ID,
so it covers every write deploy key in that repository. The controller verifies
that the declared key exists and permits writes before reading or changing
rulesets. A missing or read-only key warns and leaves protections untouched.
Review all write keys before declaring the exception.

The controller discovers repositories owned by `kellen-miller` through the public
repository list and the App installation, or the local user's accessible repos.
There is no central repository inventory. It skips archived repositories and
forks, resolves each default branch to an immutable commit SHA, then reads the
requirements file with the [Contents API](https://docs.github.com/en/rest/repos/contents).
Missing, malformed, or empty requirements produce a warning and leave existing
protections untouched. Permission, API, write, and read-back failures fail the
run while other repositories continue. Unsupported ruleset plans produce a
warning. Only the three named rulesets are managed; other rulesets and classic
branch protections remain untouched and may impose additional restrictions.

Public repositories get detailed reports. Private repositories and repositories
without explicit public metadata appear only as aggregate counts by outcome.
Their names, check names, requirements, diffs, and API errors stay out of logs,
reports, annotations, and job summaries, including with `--diff`. Use GitHub CLI
separately when private details are needed.

### Local preview

Requires Python 3.12+, GitHub CLI, and authenticated repository administration
access. No Python dependencies. Preview is the default and makes only GET calls:

```sh
python3 scripts/reconcile-rulesets.py --validate
python3 -m unittest discover -s tests -v
python3 scripts/reconcile-rulesets.py --report /tmp/rulesets-preview.txt
python3 scripts/reconcile-rulesets.py --repo .github
```

The Actions summary shows one row per public repository plus private outcome
counts. Logs omit JSON diffs by default; `--diff` includes public diffs.
`--validate` checks the local baseline and this repository's own requirements,
without API calls.

Review the preview before explicitly applying:

```sh
python3 scripts/reconcile-rulesets.py --apply --repo .github
python3 scripts/reconcile-rulesets.py --apply
```

Writes are sequential and verified by reading back each ruleset. A failed apply
can leave some rulesets updated; fix the reported error and rerun. Identical state
produces no writes. No rulesets are deleted. To roll back, disable scheduled writes
and revert the affected repository's requirements, then reconcile. Newly created
rulesets must be manually disabled or deleted if reverting their introduction.

### Bootstrap or rename required checks

For a first declaration or a check rename, pause scheduled writes by setting
`RULESETS_APPLY_ENABLED=false`. Let the candidate PR's new checks pass before
applying its requirements. Use the candidate's full commit SHA and select exactly
one repository:

```sh
python3 scripts/reconcile-rulesets.py --repo .github --requirements-ref FULL_40_CHARACTER_COMMIT_SHA
python3 scripts/reconcile-rulesets.py --apply --repo .github --requirements-ref FULL_40_CHARACTER_COMMIT_SHA
```

Review the preview, apply the candidate requirements, then merge that exact PR
head, then restore `RULESETS_APPLY_ENABLED` to its previous value. Keeping
scheduled writes paused until that merge prevents the old default-branch
declaration from undoing the bootstrap. This replaces the old check set; it does
not require old and new names at once. A missing file or invalid candidate SHA
fails bootstrap instead of silently skipping it. The override changes only where
requirements are read. The scheduled
workflow always reads default branches and never supplies this override.

### GitHub Actions setup

1. Create a dedicated GitHub App with repository **Administration: read and write**
   and **Contents: read** (Metadata read is automatic). Approve the Contents grant
   on existing installations. No webhook subscription is needed. Install it on
   **all repositories** owned by `kellen-miller`, so future private repos are covered.
2. Set repository variable `RULESETS_APP_ID` and Actions secret
   `RULESETS_APP_PRIVATE_KEY` in this repository.
3. Run **Reconcile rulesets** manually with `apply` unchecked. Review its log and
   job summary, including missing CI, plan restrictions, and preserved protections.
4. Run manually with `apply` checked when ready. Set repository variable
   `RULESETS_APPLY_ENABLED=true` to enable weekly writes. Otherwise weekly runs are
   previews. Set it back to `false` to stop scheduled writes.

The workflow runs only from main, serializes runs without cancellation, and mints
an installation token for all installed repositories. PR CI validates configuration
and tests reconciliation offline, without App secrets. It runs only on PRs;
reconciliation runs weekly (Mondays at 10:23 UTC) or manually, never on pushes or PRs. Nothing is applied
by opening or merging this PR without configuring the App and enabling writes.
