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
| `personal/main` | No deletion; PR required; merge/squash only; CI required against current main; checks exempt on branch creation | None |
| `personal/reviews` | One approval; dismiss stale approvals; approve latest reviewable push; extra approval for unattributed Copilot PRs | `kellen-miller` and `renovate-approve`, PR-only |
| `personal/force-pushes` | Block force pushes | `kellen-miller`, always |

Conversations need not be resolved. User ID `37915888` and App ID `7394` are
GitHub's IDs for these bypass identities, not the reconciliation App.
Force-push bypass does **not** bypass the independent PR or CI rules.
The Copilot field `require_extra_approval_for_unattributed_changes` is returned
by GitHub's live ruleset API, although absent from its published OpenAPI schema.
The script sends it explicitly and verifies the saved configuration after writes.

`rulesets/repositories.json` lists required GitHub Actions check names per repo.
Initial names were observed on recent PRs targeting main; review them as workflows
change. Celerity's push-only generated-contract publication check is excluded.
Checks are bound to the GitHub Actions App (`15368`). Keep always-triggered checks
here; path-filtered workflows can leave PRs blocked waiting for an absent check.
There is no universal "all workflows must pass" rule for personal repositories.

The script discovers owned public and accessible private repositories, including
forks. It also checks every configured repository, exposing App-installation gaps.
A newly discovered repo or an empty check list is an error: no rulesets are written
for that repo until its CI requirements are configured. Archived repos are reported
and skipped. Private repositories currently reject rulesets on this account's
plan; GitHub Pro is required. These failures remain visible with a nonzero exit,
and do not prevent reconciliation of other repositories.

Only the three named rulesets are managed. Other rulesets and classic branch
protections are preserved. Their restrictions still apply: Chief's existing
`default` ruleset, for example, blocks force pushes and requires conversation
resolution. Those settings need a separate migration before the desired bypass
behavior can take effect there. The script reports unmanaged rulesets for review;
it does not claim to reconcile every effective protection.

### Local preview

Requires Python 3.12+, GitHub CLI, and authenticated repository administration
access. No Python dependencies. Preview is the default and makes only GET calls:

```sh
python3 scripts/reconcile-rulesets.py --validate
python3 -m unittest discover -s tests -v
python3 scripts/reconcile-rulesets.py --report /tmp/rulesets-preview.txt
python3 scripts/reconcile-rulesets.py --repo .github
```

Review the diff before explicitly applying:

```sh
python3 scripts/reconcile-rulesets.py --apply --repo .github
python3 scripts/reconcile-rulesets.py --apply
```

Writes are sequential and verified by reading back each ruleset. A failed apply
can leave some rulesets updated; fix the reported error and rerun. Identical state
produces no writes. No rulesets are deleted. To roll back, disable scheduled writes
and revert the configuration, then reconcile; newly created rulesets must be
manually disabled/deleted if reverting their introduction.

### GitHub Actions setup

1. Create a dedicated GitHub App with repository **Administration: read and write**
   (Metadata read is automatic). No webhook subscription is needed. Install it on
   **all repositories** owned by `kellen-miller`, so future private repos are covered.
2. Set repository variable `RULESETS_APP_ID` and Actions secret
   `RULESETS_APP_PRIVATE_KEY` in this repository.
3. Run **Reconcile rulesets** manually with `apply` unchecked. Review its log and
   job summary, including missing CI, plan restrictions, and preserved protections.
4. Run manually with `apply` checked when ready. Set repository variable
   `RULESETS_APPLY_ENABLED=true` to enable daily writes. Otherwise daily runs are
   previews. Set it back to `false` to stop scheduled writes.

The workflow runs only from main, serializes runs without cancellation, and mints
an installation token for all installed repositories. PR CI validates configuration
and tests reconciliation offline, without App secrets. It runs only on PRs;
reconciliation runs daily or manually, never on pushes or PRs. Nothing is applied
by opening or merging this PR without configuring the App and enabling writes.
