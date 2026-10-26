import base64
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import importlib.util
import json
import subprocess
import tempfile
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
        self.settings = {"checks": ["test"]}
        self.repo = {"name": "demo", "owner": {"login": "kellen-miller"}, "archived": False, "fork": False,
                     "private": False, "default_branch": "main"}
        self.saved = {}
        self.calls = []
        self.unmanaged = {"id": 99, "name": "existing protection"}
        self.revision = "a" * 40
        self.deploy_keys = [{"id": 123, "read_only": False}]

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

        if "/branches/" in path:
            return {"commit": {"sha": self.revision}}

        if "/keys?" in path:
            return self.deploy_keys

        if "/contents/" in path:
            if self.settings is None:
                raise MODULE.GitHubError("Not found", status=404)

            return {"type": "file", "encoding": "base64",
                    "content": base64.b64encode(json.dumps(self.settings).encode()).decode()}

        if "rulesets?" in path:
            return [self.unmanaged, *self.saved.values()]

        if "/rulesets/" in path:
            return copy.deepcopy(self.saved[int(path.rsplit("/", 1)[-1])])

        raise AssertionError(f"Unexpected request: {path}")

    def run_reconcile(self, apply=False):
        with patch.object(MODULE, "github", side_effect=self.api):
            return MODULE.reconcile(self.baseline, apply)

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

    def test_only_owner_can_bypass_ci_through_pr(self):
        main, reviews, force = MODULE.desired_rulesets(self.baseline, ["test"])
        self.assertEqual(main["bypass_actors"], [
            {"actor_type": "User", "actor_id": 37915888, "bypass_mode": "pull_request"},
        ])
        self.assertEqual(reviews["bypass_actors"], [
            {"actor_type": "User", "actor_id": 37915888, "bypass_mode": "exempt"},
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

    def test_release_key_exception_is_scoped_idempotent_and_keeps_force_protection(self):
        self.settings["release_deploy_key_id"] = 123
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        expected = {"actor_type": "DeployKey", "actor_id": None, "bypass_mode": "always"}
        for ruleset in self.saved.values():
            if ruleset["name"] == "personal/force-pushes":
                self.assertNotIn(expected, ruleset["bypass_actors"])
                self.assertEqual(ruleset["rules"], [{"type": "non_fast_forward"}])
            else:
                self.assertIn(expected, ruleset["bypass_actors"])

        self.calls.clear()
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertEqual(report.count("OK demo:"), 3)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
        ordinary = MODULE.desired_rulesets(self.baseline, ["test"])
        self.assertTrue(all(expected not in rule["bypass_actors"] for rule in ordinary))

    def test_declared_deploy_key_must_exist_and_allow_writes(self):
        self.settings["release_deploy_key_id"] = 123
        for keys in [[], [{"id": 999, "read_only": False}], [{"id": 123, "read_only": True}], [{"id": 123}]]:
            with self.subTest(keys=keys):
                self.deploy_keys = keys
                self.calls.clear()
                self.saved = {1: dict(MODULE.desired_rulesets(self.baseline, ["original"])[0], id=1)}
                before = copy.deepcopy(self.saved)
                report, failures = self.run_reconcile(apply=True)
                self.assertEqual(failures, 0)
                self.assertIn("Declared release deploy key is missing or does not allow writes", report)
                self.assertEqual(self.saved, before)
                self.assertFalse(any("/rulesets" in path for _, path, _ in self.calls))

    def test_verified_deploy_key_allows_other_write_keys_and_precedes_ruleset_reads(self):
        self.settings["release_deploy_key_id"] = 123
        self.deploy_keys.append({"id": 456, "read_only": False})
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertIn("APPLIED demo:", report)
        paths = [path for _, path, _ in self.calls]
        self.assertLess(next(index for index, path in enumerate(paths) if "/keys?" in path),
                        next(index for index, path in enumerate(paths) if "/rulesets?" in path))

    def test_missing_or_invalid_requirements_warn_without_writes(self):
        for settings in [None, {}, {"checks": []}, {"checks": ["test", "test"]},
                         {"checks": [" "]}, {"checks": [" test"]}, {"checks": ["test\nlabel"]},
                         {"checks": [True]}, {"checks": ["test"], "extra": "unknown"},
                         {"checks": ["test"], "release_deploy_key_id": True},
                         {"checks": ["test"], "release_deploy_key_id": 0},
                         {"checks": ["test"], "release_deploy_key_id": None}]:
            with self.subTest(settings=settings):
                self.settings = settings
                self.calls.clear()
                self.saved = {1: dict(MODULE.desired_rulesets(self.baseline, ["original"])[0], id=1)}
                before = copy.deepcopy(self.saved)
                report, failures = self.run_reconcile(apply=True)
                self.assertEqual(failures, 0)
                self.assertIn("WARNING demo:", report)
                self.assertEqual(self.saved, before)
                self.assertTrue(all(method == "GET" for method, _, _ in self.calls))
                self.assertFalse(any("/rulesets" in path for _, path, _ in self.calls))

    def test_plan_restrictions_are_reported(self):
        original = self.api

        def restricted(path, method="GET", payload=None):
            if "rulesets?" in path:
                raise MODULE.UnsupportedRepository("Upgrade to GitHub Pro to enable this feature.")

            return original(path, method, payload)

        with patch.object(MODULE, "github", side_effect=restricted):
            report, failures = MODULE.reconcile(self.baseline, apply=True)

        self.assertEqual(failures, 0)
        self.assertIn("WARNING demo: Upgrade to GitHub Pro", report)
        self.assertEqual(self.saved, {})

    def test_api_only_classifies_plan_restrictions_on_initial_reads(self):
        plan = "Upgrade to GitHub Pro or make this repository public to enable this feature."
        for method, path, message, status, expected in [
            ("GET", "repos/kellen-miller/demo/rulesets?page=1", plan, 403, MODULE.UnsupportedRepository),
            ("GET", "repos/kellen-miller/demo/rulesets?page=1", "Resource not accessible by integration", 403, MODULE.GitHubError),
            ("GET", "repos/kellen-miller/demo/rulesets?page=1", plan, 500, MODULE.GitHubError),
            ("POST", "repos/kellen-miller/demo/rulesets", plan, 403, MODULE.GitHubError),
        ]:
            with self.subTest(method=method, message=message, status=status):
                response = subprocess.CompletedProcess([], 1, json.dumps({"message": message}), f"gh: {message} (HTTP {status})")
                with patch.object(MODULE.subprocess, "run", return_value=response):
                    with self.assertRaises(expected) as raised:
                        MODULE.github(path, method)

                self.assertIs(type(raised.exception), expected)

    def test_github_parses_actual_cli_http_status_errors(self):
        path = f"repos/kellen-miller/demo/contents/{MODULE.REQUIREMENTS_PATH}?ref={self.revision}"
        for status in [404, 409]:
            with self.subTest(status=status):
                response = subprocess.CompletedProcess([], 1, '{"message":"Not Found"}',
                                                       f"gh: Not Found (HTTP {status})\n")
                with patch.object(MODULE.subprocess, "run", return_value=response):
                    with self.assertRaises(MODULE.GitHubError) as raised:
                        MODULE.github(path)

                self.assertEqual(raised.exception.status, status)

    def test_permission_errors_still_fail(self):
        original = self.api

        def denied(path, method="GET", payload=None):
            if "rulesets?" in path:
                raise MODULE.GitHubError("Resource not accessible by integration (HTTP 403)")

            return original(path, method, payload)

        with patch.object(MODULE, "github", side_effect=denied):
            report, failures = MODULE.reconcile(self.baseline, apply=True)

        self.assertEqual(failures, 1)
        self.assertIn("ERROR demo:", report)
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
            report, failures = MODULE.reconcile(self.baseline, apply=True)

        self.assertEqual(failures, 1)
        self.assertIn("Read-back verification failed", report)

    def test_app_discovery_includes_private_without_user_fallback(self):
        original = self.api
        private = dict(self.repo, name="private-demo", private=True)

        def installation(path, method="GET", payload=None):
            if path.startswith("installation/repositories"):
                return {"repositories": [self.repo, private]}

            return original(path, method, payload)

        with patch.object(MODULE, "github", side_effect=installation):
            report, failures = MODULE.reconcile(self.baseline)

        self.assertEqual(failures, 0)
        self.assertNotIn("private-demo", report)
        self.assertIn("PRIVATE: 1 repositories; 1 planned", report)
        self.assertFalse(any(path.startswith("user/repos?") for _, path, _ in self.calls))

    def test_summary_groups_outcomes_and_escapes_table_content(self):
        report = "\n".join([
            "APPLY: 5 repositories, 1 warnings, 1 errors",
            "UPDATE demo: personal/reviews", "APPLIED demo: personal/reviews",
            "OK demo: personal/main", "OK demo: personal/force-pushes",
            "WARNING private-demo: Upgrade to Pro | plan <limited>",
            "SKIP fork: fork", "ERROR denied: permission denied",
            "OK stable: personal/main", "OK stable: personal/reviews",
            '+  "bypass_actors": []',
        ])
        summary = MODULE.summary_table(report)
        self.assertEqual(summary.count("| demo |"), 1)
        self.assertIn("| demo | Applied | Applied: 1 rulesets; Unchanged: 2 rulesets |", summary)
        self.assertIn("| private-demo | Warning | Upgrade to Pro &#124; plan &lt;limited&gt; |", summary)
        self.assertIn("| fork | Skipped | fork |", summary)
        self.assertIn("| denied | Failed | permission denied |", summary)
        self.assertIn("| stable | Unchanged | Unchanged: 2 rulesets |", summary)
        self.assertNotIn("bypass_actors", summary)
        self.assertNotIn("```", summary)

    def test_json_diffs_are_opt_in(self):
        report, _ = self.run_reconcile()
        self.assertNotIn("--- current", report)
        with patch.object(MODULE, "github", side_effect=self.api):
            detailed, failures = MODULE.reconcile(self.baseline, show_diff=True)

        self.assertEqual(failures, 0)
        self.assertIn("--- current", detailed)
        self.assertNotIn("bypass_actors", MODULE.summary_table(detailed))
        self.assertIn("| demo | Changes planned |", MODULE.summary_table(detailed))

    def test_pagination_keeps_all_repositories(self):
        with patch.object(MODULE, "github", side_effect=[list(range(100)), [100]]) as request:
            self.assertEqual(MODULE.pages("repos/example/checks"), list(range(101)))

        self.assertEqual(request.call_count, 2)
        self.assertIn("page=2", request.call_args.args[0])

    def test_default_branch_is_resolved_once_then_contents_use_immutable_sha(self):
        self.repo["default_branch"] = "release/stable"
        self.run_reconcile()
        paths = [path for _, path, _ in self.calls]
        self.assertIn("repos/kellen-miller/demo/branches/release%2Fstable", paths)
        self.assertIn(f"repos/kellen-miller/demo/contents/{MODULE.REQUIREMENTS_PATH}?ref={self.revision}", paths)
        self.assertEqual(sum("/branches/" in path for path in paths), 1)
        self.assertFalse(any("/commits/" in path for path in paths))

    def test_missing_or_empty_default_branch_warns_other_branch_errors_fail(self):
        for status in [404, 409, 403, 500]:
            with self.subTest(status=status):
                self.calls.clear()
                original = self.api

                def branch_error(path, method="GET", payload=None):
                    if "/branches/" in path:
                        raise MODULE.GitHubError("Branch lookup failed", status=status)

                    return original(path, method, payload)

                with patch.object(MODULE, "github", side_effect=branch_error):
                    report, failures = MODULE.reconcile(self.baseline, apply=True)

                self.assertEqual(failures, int(status not in {404, 409}))
                self.assertIn("WARNING demo:" if status in {404, 409} else "ERROR demo:", report)
                self.assertFalse(any("/contents/" in path or "/rulesets" in path for _, path, _ in self.calls))
                self.assertEqual(self.saved, {})

    def test_bootstrap_ref_requires_one_discovered_repo_and_full_sha(self):
        for selected, revision in [(None, self.revision), (["demo", "demo"], self.revision),
                                   (["demo"], "main"), (["demo"], "a" * 39),
                                   (["demo"], "z" * 40), (["not-discovered"], self.revision)]:
            with self.subTest(selected=selected, revision=revision):
                with patch.object(MODULE, "github", side_effect=self.api):
                    with self.assertRaises(ValueError):
                        MODULE.reconcile(self.baseline, selected=selected, requirements_ref=revision)

        revision = "b" * 40
        self.calls.clear()
        with patch.object(MODULE, "github", side_effect=self.api):
            report, failures = MODULE.reconcile(self.baseline, selected=["demo"], requirements_ref=revision)

        self.assertEqual(failures, 0)
        self.assertIn("CREATE demo:", report)
        self.assertFalse(any("/branches/" in path for _, path, _ in self.calls))
        self.assertTrue(any(path.endswith(f"?ref={revision}") for _, path, _ in self.calls))

    def test_missing_bootstrap_requirements_is_an_error_without_writes(self):
        self.settings = None
        with patch.object(MODULE, "github", side_effect=self.api):
            report, failures = MODULE.reconcile(self.baseline, apply=True, selected=["demo"], requirements_ref=self.revision)

        self.assertEqual(failures, 1)
        self.assertIn("ERROR demo: Bootstrap requirements were not found", report)
        self.assertNotIn("WARNING demo:", report)
        self.assertEqual(self.saved, {})
        self.assertFalse(any("/rulesets" in path for _, path, _ in self.calls))

    def test_malformed_content_envelopes_cannot_change_protection(self):
        for content in [[], {}, {"type": "symlink", "encoding": "base64", "content": "e30="},
                        {"type": "file", "encoding": "none", "content": "{}"},
                        {"type": "file", "encoding": "base64", "content": "bad!"},
                        {"type": "file", "encoding": "base64", "content": "bm90IGpzb24="}]:
            with self.subTest(content=content):
                original = self.api

                def malformed(path, method="GET", payload=None):
                    if "/contents/" in path:
                        return content

                    return original(path, method, payload)

                with patch.object(MODULE, "github", side_effect=malformed):
                    report, failures = MODULE.reconcile(self.baseline, apply=True)

                self.assertEqual(failures, 0)
                self.assertIn("WARNING demo:", report)
                self.assertEqual(self.saved, {})

    def test_contents_permission_errors_fail_without_writes(self):
        original = self.api

        def denied(path, method="GET", payload=None):
            if "/contents/" in path:
                raise MODULE.GitHubError("Contents permission denied", status=403)

            return original(path, method, payload)

        with patch.object(MODULE, "github", side_effect=denied):
            report, failures = MODULE.reconcile(self.baseline, apply=True)

        self.assertEqual(failures, 1)
        self.assertIn("ERROR demo: Contents permission denied", report)
        self.assertEqual(self.saved, {})

    def test_invalid_default_commit_sha_fails_without_reading_requirements(self):
        self.revision = "main"
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 1)
        self.assertIn("full commit SHA", report)
        self.assertFalse(any("/contents/" in path for _, path, _ in self.calls))
        self.assertEqual(self.saved, {})

    def test_private_and_unknown_metadata_never_publish_details(self):
        self.repo["name"] = "private-demo"
        self.settings = {"checks": ["confidential-check"]}
        self.unmanaged["name"] = "confidential-rule"
        for visibility in [{"private": True}, {}, {"private": False, "visibility": "private"}]:
            with self.subTest(visibility=visibility):
                self.repo.pop("private", None)
                self.repo.pop("visibility", None)
                self.repo.update(visibility)
                for stage, failure in [
                    ("none", None),
                    ("read", MODULE.GitHubError("https://api.github.com/repos/kellen-miller/private-demo confidential-check")),
                    ("write", MODULE.GitHubError("https://api.github.com/repos/kellen-miller/private-demo confidential-rule")),
                    ("read", subprocess.TimeoutExpired(["gh", "repos/kellen-miller/private-demo"], 60)),
                ]:
                    self.saved.clear()
                    original = self.api

                    def private_api(path, method="GET", payload=None):
                        if failure is not None and (
                            (stage == "read" and "/rulesets?" in path) or (stage == "write" and method == "POST")
                        ):
                            raise failure

                        return original(path, method, payload)

                    with tempfile.TemporaryDirectory() as directory:
                        report_path = Path(directory) / "report.txt"
                        summary_path = Path(directory) / "summary.md"
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with patch.object(MODULE, "github", side_effect=private_api), \
                                patch.object(MODULE.sys, "argv", ["reconcile", "--apply", "--diff", "--report", str(report_path)]), \
                                patch.dict(MODULE.os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary_path)}), \
                                redirect_stdout(stdout), redirect_stderr(stderr):
                            status = MODULE.main()

                        self.assertEqual(status, int(failure is not None))
                        output = stdout.getvalue() + stderr.getvalue() + report_path.read_text() + summary_path.read_text()
                        for secret in ["private-demo", "confidential-check", "confidential-rule", "api.github.com", "repos/kellen-miller/"]:
                            self.assertNotIn(secret, output)

                        self.assertIn("PRIVATE: 1 repositories; 1", output)
                        self.assertNotIn("--- current", output)

    def test_private_missing_requirements_warn_only_as_aggregate(self):
        self.repo.update(name="private-demo", private=True)
        self.settings = None
        report, failures = self.run_reconcile(apply=True)
        self.assertEqual(failures, 0)
        self.assertIn("PRIVATE: 1 repositories; 1 warnings", report)
        self.assertNotIn("private-demo", report + MODULE.summary_table(report))
        self.assertEqual(self.saved, {})

    def test_mixed_run_retains_public_details_and_emits_only_count_private_warning(self):
        private = dict(self.repo, name="private-demo", private=True)
        original = self.api

        def mixed(path, method="GET", payload=None):
            if path.startswith("installation/repositories"):
                return {"repositories": [self.repo, private]}

            if "/private-demo/contents/" in path:
                raise MODULE.GitHubError("https://api.github.com/repos/kellen-miller/private-demo confidential-check", status=404)

            return original(path, method, payload)

        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.txt"
            summary_path = Path(directory) / "summary.md"
            stdout = io.StringIO()
            with patch.object(MODULE, "github", side_effect=mixed), \
                    patch.object(MODULE.sys, "argv", ["reconcile", "--diff", "--report", str(report_path)]), \
                    patch.dict(MODULE.os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary_path)}), \
                    redirect_stdout(stdout):
                self.assertEqual(MODULE.main(), 0)

            summary = summary_path.read_text()
            output = stdout.getvalue() + report_path.read_text() + summary
            self.assertIn("| demo | Changes planned |", summary)
            self.assertIn("PRIVATE: 1 repositories; 1 warnings", summary)
            self.assertIn("--- current", stdout.getvalue())
            self.assertIn("::warning title=Private repositories skipped::1 private repositories require attention", stdout.getvalue())
            for secret in ["private-demo", "confidential-check", "api.github.com"]:
                self.assertNotIn(secret, output)

    def test_discovery_error_discards_unknown_private_metadata(self):
        with patch.object(MODULE, "github", side_effect=MODULE.GitHubError("repos/kellen-miller/private-demo")):
            with self.assertRaises(MODULE.GitHubError) as raised:
                MODULE.reconcile(self.baseline)

        self.assertNotIn("private-demo", str(raised.exception))
        self.assertIn("Repository discovery failed", str(raised.exception))

    def test_validate_checks_only_local_baseline_and_own_requirements(self):
        stdout = io.StringIO()
        with patch.object(MODULE, "github") as api, patch.object(MODULE.sys, "argv", ["reconcile", "--validate"]), \
                redirect_stdout(stdout):
            self.assertEqual(MODULE.main(), 0)

        api.assert_not_called()
        self.assertIn("requirements are valid", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
