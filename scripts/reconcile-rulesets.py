#!/usr/bin/env python3
"""Reconcile only the three personal rulesets; default to read-only preview."""

import argparse
import base64
import binascii
import copy
import difflib
import html
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "2026-03-10"
OWNER = "kellen-miller"
REQUIREMENTS_PATH = ".github/ruleset-requirements.json"


class GitHubError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class UnsupportedRepository(GitHubError):
    pass


class InvalidRequirements(ValueError):
    pass


def github(path, method="GET", payload=None):
    command = [
        "gh", "api", "--method", method,
        "-H", f"X-GitHub-Api-Version: {API_VERSION}", path,
    ]
    if payload is not None:
        command.extend(["--input", "-"])

    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        text=True, capture_output=True, timeout=60, check=False,
    )
    if result.returncode:
        try:
            message = json.loads(result.stdout).get("message", "")
        except json.JSONDecodeError:
            message = ""

        if (method == "GET" and "/rulesets?" in path
                and "(HTTP 403)" in result.stderr
                and message.startswith("Upgrade to GitHub ")
                and message.endswith("to enable this feature.")):
            raise UnsupportedRepository(message)

        status = re.search(r"\(HTTP (\d+)\)", result.stderr)
        raise GitHubError(
            f"{method} {path}: {result.stderr.strip()}",
            int(status[1]) if status else None,
        )

    return json.loads(result.stdout)


def pages(path, key=None):
    items = []
    for page in range(1, 1001):
        separator = "&" if "?" in path else "?"
        response = github(f"{path}{separator}per_page=100&page={page}")
        batch = response[key] if key else response
        items.extend(batch)
        if len(batch) < 100:
            return items

    raise GitHubError(f"Pagination limit reached: {path}")


def load_configuration(root):
    baseline = json.loads((root / "rulesets/baseline.json").read_text())
    names = [rule["name"] for rule in baseline]
    if sorted(names) != sorted([
        "personal/main", "personal/reviews", "personal/force-pushes",
    ]):
        raise ValueError("Expected exactly three uniquely named managed rulesets")

    for ruleset in baseline:
        if ruleset["conditions"] != {
            "ref_name": {"include": ["refs/heads/main"], "exclude": []},
        } or ruleset["target"] != "branch" or ruleset["enforcement"] != "active":
            raise ValueError("Managed rulesets must actively target main")

    requirements = validate_requirements(json.loads((root / REQUIREMENTS_PATH).read_text()))
    return baseline, requirements


def validate_requirements(settings):
    """Validate one repository's declaration without rendering its contents."""
    if not isinstance(settings, dict) or set(settings) - {"checks", "release_deploy_key_id"}:
        raise InvalidRequirements("Requirements must contain checks and an optional release deploy key")

    checks = settings.get("checks")
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, str) or not check.strip() or check != check.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in check)
        for check in checks
    ) or len(checks) != len(set(checks)):
        raise InvalidRequirements("Requirements need nonempty, unique check names without surrounding whitespace or controls")

    if "release_deploy_key_id" in settings:
        key = settings["release_deploy_key_id"]
        if type(key) is not int or key <= 0:
            raise InvalidRequirements("Release deploy key must be a positive integer")

    return settings


def fetch_requirements(prefix, repository, requirements_ref):
    """Read a declaration through GitHub's Contents API at one immutable commit."""
    revision = requirements_ref
    if revision is None:
        branch = repository.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise InvalidRequirements("Default branch metadata is missing")

        try:
            response = github(f"{prefix}/branches/{quote(branch, safe='')}")
        except GitHubError as error:
            if error.status in {404, 409}:
                raise InvalidRequirements("Default branch is missing or has no commits") from None

            raise

        commit = response.get("commit") if isinstance(response, dict) else None
        revision = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            raise GitHubError("Default branch did not resolve to a full commit SHA")

    try:
        content = github(f"{prefix}/contents/{REQUIREMENTS_PATH}?ref={revision}")
    except GitHubError as error:
        if error.status == 404:
            if requirements_ref is not None:
                raise GitHubError("Bootstrap requirements were not found at the requested commit") from None

            raise InvalidRequirements("Requirements file is missing") from None

        raise

    try:
        if content.get("type") != "file" or content.get("encoding") != "base64":
            raise InvalidRequirements("Requirements must be a base64 encoded JSON file")

        decoded = base64.b64decode("".join(content["content"].split()), validate=True)
        return validate_requirements(json.loads(decoded))
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise InvalidRequirements("Requirements file is malformed or invalid") from None


def desired_rulesets(baseline, checks, release_deploy_key_id=None):
    desired = copy.deepcopy(baseline)
    main = next(ruleset for ruleset in desired if ruleset["name"] == "personal/main")
    main["rules"].append({
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "do_not_enforce_on_create": True,
            "required_status_checks": [
                {"context": check, "integration_id": 15368} for check in checks
            ],
        },
    })
    if release_deploy_key_id is not None:
        for ruleset in desired:
            if ruleset["name"] in {"personal/main", "personal/reviews"}:
                ruleset["bypass_actors"].append({
                    "actor_type": "DeployKey",
                    "actor_id": None,
                    "bypass_mode": "always",
                })

    return desired


def normalized(ruleset):
    """Drop API metadata/defaults and ignore ordering of rule sets."""
    value = {key: copy.deepcopy(ruleset[key]) for key in (
        "name", "target", "enforcement", "conditions", "bypass_actors", "rules",
    )}
    for rule in value["rules"]:
        parameters = rule.get("parameters", {})
        if parameters.get("required_reviewers") == []:
            del parameters["required_reviewers"]

        if rule["type"] == "pull_request":
            parameters.setdefault("require_extra_approval_for_unattributed_changes", True)

    def ordered(item):
        if isinstance(item, dict):
            return {key: ordered(val) for key, val in item.items()}

        if isinstance(item, list):
            return sorted((ordered(val) for val in item), key=lambda val: json.dumps(val, sort_keys=True))

        return item

    return ordered(value)


def reconcile(baseline, apply=False, selected=None, show_diff=False, requirements_ref=None):
    if requirements_ref is not None and (
        not re.fullmatch(r"[0-9a-fA-F]{40}", requirements_ref) or not selected or len(selected) != 1
    ):
        raise ValueError("--requirements-ref needs a full 40-character commit SHA and exactly one --repo")

    # Discovery errors can contain private metadata. Do not forward their raw text.
    try:
        repositories = {repo["name"]: repo for repo in pages(f"users/{OWNER}/repos?type=owner")}
        try:
            accessible = pages("installation/repositories", "repositories")
        except GitHubError:
            accessible = pages("user/repos?affiliation=owner")

        repositories.update({
            repo["name"]: repo for repo in accessible if repo["owner"]["login"] == OWNER
        })
    except (GitHubError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise GitHubError("Repository discovery failed; check token access and permissions") from None

    names = set(repositories)
    if selected:
        if not set(selected) <= names:
            raise ValueError("Selected repository was not discovered in the owner's accessible repositories")

        names = set(selected)

    report = []
    private_outcomes = {}
    failures = 0
    warnings = 0
    for name in sorted(names):
        repository = repositories[name]
        public = repository.get("private") is False and repository.get("visibility") in (None, "public")
        prefix = f"repos/{OWNER}/{name}"
        details = []
        outcome = "unchanged"
        try:
            if repository["owner"]["login"] != OWNER:
                raise ValueError("Repository is no longer owned by the configured account")

            if repository["archived"]:
                outcome = "skipped"
                details.append(f"SKIP {name}: archived (read-only)")
                continue

            if repository["fork"]:
                outcome = "skipped"
                details.append(f"SKIP {name}: fork")
                continue

            settings = fetch_requirements(prefix, repository, requirements_ref)
            deploy_key_id = settings.get("release_deploy_key_id")
            if deploy_key_id is not None:
                deploy_keys = pages(f"{prefix}/keys")
                if not any(key.get("id") == deploy_key_id and key.get("read_only") is False for key in deploy_keys):
                    raise InvalidRequirements("Declared release deploy key is missing or does not allow writes")

            entries = pages(f"{prefix}/rulesets?includes_parents=false")
            desired = desired_rulesets(
                baseline, settings["checks"], settings.get("release_deploy_key_id"),
            )
            existing = {}
            for entry in entries:
                if entry["name"] in {rule["name"] for rule in desired}:
                    if entry["name"] in existing:
                        raise ValueError(f"Duplicate managed ruleset: {entry['name']}")

                    existing[entry["name"]] = github(f"{prefix}/rulesets/{entry['id']}")
                else:
                    details.append(
                        f"KEEP {name}: unmanaged ruleset {entry['name']} "
                        "may impose additional restrictions/bypass limits"
                    )

            # Complete repository reads before its first write.
            for ruleset in desired:
                current = existing.get(ruleset["name"])
                if current and normalized(current) == normalized(ruleset):
                    details.append(f"OK {name}: {ruleset['name']}")
                    continue

                action = "UPDATE" if current else "CREATE"
                details.append(f"{action} {name}: {ruleset['name']}")
                if show_diff:
                    before = json.dumps(normalized(current), indent=2, sort_keys=True) if current else "{}"
                    after = json.dumps(normalized(ruleset), indent=2, sort_keys=True)
                    details.extend(difflib.unified_diff(
                        before.splitlines(), after.splitlines(), fromfile="current",
                        tofile="desired", lineterm="",
                    ))


                if apply:
                    path = f"{prefix}/rulesets"
                    if current:
                        path += f"/{current['id']}"

                    result = github(path, "PUT" if current else "POST", ruleset)
                    saved = github(f"{prefix}/rulesets/{result['id']}")
                    if normalized(saved) != normalized(ruleset):
                        raise GitHubError(f"Read-back verification failed: {ruleset['name']}")

                    details.append(f"APPLIED {name}: {ruleset['name']}")

            if any(line.startswith("APPLIED ") for line in details):
                outcome = "applied"
            elif any(line.startswith(("CREATE ", "UPDATE ")) for line in details):
                outcome = "planned"

        except (UnsupportedRepository, InvalidRequirements) as error:
            warnings += 1
            outcome = "warnings"
            details.append(f"WARNING {name}: {error}; no rulesets changed")

        except (GitHubError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            failures += 1
            outcome = "errors"
            details.append(f"ERROR {name}: {error}")

        finally:
            # Publish only explicitly public metadata. Private/unknown details,
            # including diffs and raw operation errors, never leave this buffer.
            if public:
                report.extend(details)
            else:
                private_outcomes[outcome] = private_outcomes.get(outcome, 0) + 1

    if private_outcomes:
        counts = "; ".join(f"{private_outcomes[key]} {key}" for key in sorted(private_outcomes))
        report.append(f"PRIVATE: {sum(private_outcomes.values())} repositories; {counts}")

    report.insert(0, (
        f"{'APPLY' if apply else 'DRY RUN'}: {len(names)} repositories, "
        f"{warnings} warnings, {failures} errors"
    ))
    return "\n".join(report) + "\n", failures


def summary_table(report):
    """Map reconciliation outcomes to one readable row per repository."""
    repositories = {}
    events = {"OK", "CREATE", "UPDATE", "APPLIED", "KEEP", "WARNING", "SKIP", "ERROR"}
    for line in report.splitlines()[1:]:
        event, _, remainder = line.partition(" ")
        if event not in events:
            continue

        name, separator, detail = remainder.partition(": ")
        if not separator:
            continue

        outcomes = repositories.setdefault(name, {})
        outcomes.setdefault(event, []).append(detail)

    lines = [
        "## Repository ruleset reconciliation", "", report.splitlines()[0], "",
        "| Repository | Result | Details |", "| --- | --- | --- |",
    ]
    for line in report.splitlines():
        if line.startswith("PRIVATE: "):
            lines.insert(4, line)
            lines.insert(5, "")

    for name, outcomes in repositories.items():
        details = []
        if "ERROR" in outcomes:
            result = "Failed"
        elif "WARNING" in outcomes:
            result = "Warning"
        elif "SKIP" in outcomes:
            result = "Skipped"
        elif "APPLIED" in outcomes:
            result = "Applied"
        elif "CREATE" in outcomes or "UPDATE" in outcomes:
            result = "Changes planned"
        else:
            result = "Unchanged"

        for event, label in [("APPLIED", "Applied"), ("OK", "Unchanged")]:
            if event in outcomes:
                details.append(f"{label}: {len(outcomes[event])} rulesets")

        if "APPLIED" not in outcomes:
            for event, label in [("CREATE", "Create"), ("UPDATE", "Update")]:
                if event in outcomes:
                    details.append(f"{label}: {', '.join(outcomes[event])}")

        for event in ["ERROR", "WARNING", "SKIP", "KEEP"]:
            details.extend(outcomes.get(event, []))

        safe_name = html.escape(name).replace("|", "&#124;")
        safe_details = "; ".join(html.escape(detail).replace("|", "&#124;") for detail in details)
        lines.append(f"| {safe_name} | {result} | {safe_details} |")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", action="store_true", help="Include full JSON diffs in logs/report")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    parser.add_argument("--validate", action="store_true", help="Validate configuration offline")
    parser.add_argument("--repo", action="append", help="Limit to an owned repo; repeatable")
    parser.add_argument("--report", type=Path, help="Save the privacy-filtered reconciliation report")
    parser.add_argument("--requirements-ref", help="Read requirements at a full commit SHA for exactly one --repo")
    args = parser.parse_args()
    baseline, _ = load_configuration(ROOT)
    if args.validate:
        if args.apply:
            parser.error("--validate and --apply are mutually exclusive")

        print("Shared baseline and this repository's requirements are valid")
        return 0

    report, failures = reconcile(baseline, args.apply, args.repo, args.diff, args.requirements_ref)
    print(report, end="")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in report.splitlines():
            if line.startswith("WARNING "):
                message = line.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                print(f"::warning title=Repository skipped::{message}")
            elif line.startswith("PRIVATE: "):
                warning = re.search(r"; (\d+) warnings(?:;|$)", line)
                if warning:
                    print(f"::warning title=Private repositories skipped::{warning[1]} private repositories require attention")

    if args.report:
        args.report.write_text(report)

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(summary_table(report))

    return int(bool(failures))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GitHubError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
