import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("reconcile", ROOT / "scripts/reconcile-rulesets.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.baseline, _ = MODULE.load_configuration(ROOT)
        self.config = {"owner": "kellen-miller", "repositories": {"demo": {"checks": ["test"]}}}
        self.repo = {"name": "demo", "owner": {"login": "kellen-miller"}, "archived": False, "fork": False}
        self.saved = {}
        self.calls = []
        self.unmanaged = {"id": 99, "name": "existing protection"}

    def api(self, path, method="GET", payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method in ("POST", "PUT"):
            ruleset = copy.deepcopy(payload)
            ruleset["id"] = int(path.rsplit("/", 1)[-1]) if method == "PUT" else len(self.saved) + 1
            self.saved[ruleset["id"]] = ruleset
            return ruleset

        if path.startswith("users/") or path.startswith("user/repos?"):
            return [self.repo]

        if path.startswith("installation/repositories"):
            raise MODULE.GitHubError("Not an App token")

        if "rulesets?" in path:
            return [self.unmanaged, *self.saved.values()]

        if "/rulesets/" in path:
            return copy.deepcopy(self.saved[int(path.rsplit("/", 1)[-1])])

        raise AssertionError(f"Unexpected request: {path}")

    def run_reconcile(self, apply=False):
        with patch.object(MODULE, "github", side_effect=self.api):
            return MODULE.reconcile(self.baseline, self.config, apply)

    def test_dry_run_never_writes(self):
        report, failures = self.run_reconcile()
        self.assertEqual(failures, 0)
        self.assertEqual(report.count("CREATE demo:"), 3)
        self.assertIn("KEEP demo: unmanaged", report)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_apply_is_idempotent_and_preserves_unmanaged(self):
        _, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertEqual(len(self.saved), 3)
        self.assertNotIn(99, self.saved)
        self.calls.clear()
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertEqual(report.count("OK demo:"), 3)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_update_repairs_managed_drift(self):
        self.run_reconcile(apply=True)
        self.saved[1]["enforcement"] = "disabled"
        self.calls.clear()
        self.run_reconcile(apply=True)
        writes = [(method, path) for method, path, _ in self.calls if method != "GET"]
        self.assertEqual(writes, [("PUT", "repos/kellen-miller/demo/rulesets/1")])
        self.assertEqual(self.saved[1]["enforcement"], "active")

    def test_bypasses_cannot_skip_ci_or_pr(self):
        main, reviews, force = MODULE.desired_rulesets(self.baseline, ["test"])
        self.assertEqual(main["bypass_actors"], [])
        self.assertEqual(reviews["bypass_actors"], [
            {"actor_type": "User", "actor_id": 37915888, "bypass_mode": "pull_request"},
            {"actor_type": "Integration", "actor_id": 7394, "bypass_mode": "pull_request"},
        ])
        self.assertEqual(force["bypass_actors"], [
            {"actor_type": "User", "actor_id": 37915888, "bypass_mode": "always"},
        ])
        self.assertEqual(force["rules"], [{"type": "non_fast_forward"}])
        self.assertEqual(main["rules"][-1]["parameters"], {
            "strict_required_status_checks_policy": True,
            "do_not_enforce_on_create": True,
            "required_status_checks": [{"context": "test", "integration_id": 15368}],
        })
        self.assertTrue(reviews["rules"][0]["parameters"]["require_extra_approval_for_unattributed_changes"])

    def test_missing_checks_and_new_repos_fail_without_writes(self):
        for repositories in ({}, {"demo": {"checks": []}}):
            self.config["repositories"] = repositories
            self.calls.clear()
            report, failures = self.run_reconcile(apply=True)
            self.assertEqual(failures, 1)
            self.assertIn("CI checks not configured", report)
            self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_plan_restrictions_are_reported(self):
        original = self.api

        def restricted(path, method="GET", payload=None):
            if "rulesets?" in path:
                raise MODULE.GitHubError("Upgrade to GitHub Pro (HTTP 403)")

            return original(path, method, payload)

        with patch.object(MODULE, "github", side_effect=restricted):
            report, failures = MODULE.reconcile(self.baseline, self.config, apply=True)

        self.assertEqual(failures, 1)
        self.assertIn("Upgrade to GitHub Pro", report)
        self.assertEqual(self.saved, {})

    def test_archived_repos_are_skipped(self):
        self.repo["archived"] = True
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertIn("SKIP demo: archived", report)
        self.assertEqual(self.saved, {})

    def test_forks_are_skipped_before_ruleset_reads(self):
        self.repo["fork"] = True
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertIn("SKIP demo: fork", report)
        self.assertFalse(any("/rulesets" in path for _, path, _ in self.calls))
        self.assertEqual(self.saved, {})

    def test_duplicate_managed_names_fail_before_writes(self):
        desired = MODULE.desired_rulesets(self.baseline, ["test"])[0]
        self.saved = {1: dict(desired, id=1), 2: dict(desired, id=2)}
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 1)
        self.assertIn("Duplicate managed ruleset", report)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))

    def test_readback_detects_ignored_copilot_field(self):
        original = self.api

        def ignored(path, method="GET", payload=None):
            result = original(path, method, payload)
            if method == "GET" and "/rulesets/" in path:
                for rule in result["rules"]:
                    if rule["type"] == "pull_request":
                        rule["parameters"]["require_extra_approval_for_unattributed_changes"] = False

            return result

        with patch.object(MODULE, "github", side_effect=ignored):
            report, failures = MODULE.reconcile(self.baseline, self.config, apply=True)

        self.assertEqual(failures, 1)
        self.assertIn("Read-back verification failed", report)

    def test_app_discovery_includes_private_and_unconfigured(self):
        original = self.api
        private = dict(self.repo, name="private-demo")

        def installation(path, method="GET", payload=None):
            if path.startswith("installation/repositories"):
                return {"repositories": [self.repo, private]}

            return original(path, method, payload)

        with patch.object(MODULE, "github", side_effect=installation):
            report, failures = MODULE.reconcile(self.baseline, self.config)

        self.assertEqual(failures, 1)
        self.assertIn("ERROR private-demo: CI checks not configured", report)
        self.assertFalse(any(path.startswith("user/repos?") for _, path, _ in self.calls))

    def test_pagination_keeps_all_repositories(self):
        with patch.object(MODULE, "github", side_effect=[list(range(100)), [100]]) as request:
            self.assertEqual(MODULE.pages("repos/example/checks"), list(range(101)))

        self.assertEqual(request.call_count, 2)
        self.assertIn("page=2", request.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
