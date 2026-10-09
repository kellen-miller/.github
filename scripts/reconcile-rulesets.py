#!/usr/bin/env python3
"""Reconcile only the three personal rulesets; default to read-only preview."""

import argparse
import copy
import difflib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "2026-03-10"


class GitHubError(RuntimeError):
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
        raise GitHubError(f"{method} {path}: {result.stderr.strip()}")

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
    config = json.loads((root / "rulesets/repositories.json").read_text())
    if config["owner"] != "kellen-miller":
        raise ValueError("The baseline's bypass identities belong to kellen-miller")

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

    for name, repository in config["repositories"].items():
        if not name or "/" in name:
            raise ValueError(f"Invalid repository name: {name}")

        checks = repository["checks"]
        if not isinstance(checks, list) or any(
            not isinstance(check, str) or not check.strip() for check in checks
        ) or len(checks) != len(set(checks)):
            raise ValueError(f"Invalid or duplicate checks: {name}")

    return baseline, config


def desired_rulesets(baseline, checks):
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


def reconcile(baseline, config, apply=False, selected=None):
    owner = config["owner"]
    # Public discovery catches new repos missing from a selected App installation.
    repositories = {repo["name"]: repo for repo in pages(f"users/{owner}/repos?type=owner")}
    try:
        accessible = pages("installation/repositories", "repositories")
    except GitHubError:
        # Local gh authentication uses a user token, not an installation token.
        accessible = pages("user/repos?affiliation=owner")

    repositories.update({
        repo["name"]: repo for repo in accessible if repo["owner"]["login"] == owner
    })
    names = set(repositories) | set(config["repositories"])
    if selected:
        if not set(selected) <= names:
            raise ValueError("Selected repository is not owned or configured")

        names = set(selected)

    report = []
    failures = 0
    for name in sorted(names):
        prefix = f"repos/{owner}/{name}"
        try:
            repository = repositories.get(name) or github(prefix)
            if repository["owner"]["login"] != owner:
                raise ValueError(f"Repository is no longer owned by {owner}")

            if repository["archived"]:
                report.append(f"SKIP {name}: archived (read-only)")
                continue

            if repository["fork"]:
                report.append(f"SKIP {name}: fork")
                continue

            entries = pages(f"{prefix}/rulesets?includes_parents=false")
            settings = config["repositories"].get(name)
            if not settings or not settings["checks"]:
                raise ValueError("CI checks not configured; no rulesets changed")

            desired = desired_rulesets(baseline, settings["checks"])
            existing = {}
            for entry in entries:
                if entry["name"] in {rule["name"] for rule in desired}:
                    if entry["name"] in existing:
                        raise ValueError(f"Duplicate managed ruleset: {entry['name']}")

                    existing[entry["name"]] = github(f"{prefix}/rulesets/{entry['id']}")
                else:
                    report.append(
                        f"KEEP {name}: unmanaged ruleset {entry['name']} "
                        "may impose additional restrictions/bypass limits"
                    )

            # Complete repository reads before its first write.
            for ruleset in desired:
                current = existing.get(ruleset["name"])
                if current and normalized(current) == normalized(ruleset):
                    report.append(f"OK {name}: {ruleset['name']}")
                    continue

                action = "UPDATE" if current else "CREATE"
                report.append(f"{action} {name}: {ruleset['name']}")
                before = json.dumps(normalized(current), indent=2, sort_keys=True) if current else "{}"
                after = json.dumps(normalized(ruleset), indent=2, sort_keys=True)
                report.extend(difflib.unified_diff(
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

                    report.append(f"APPLIED {name}: {ruleset['name']}")

        except (GitHubError, ValueError, subprocess.TimeoutExpired) as error:
            failures += 1
            report.append(f"ERROR {name}: {error}")

    report.insert(0, f"{'APPLY' if apply else 'DRY RUN'}: {len(names)} repositories, {failures} errors")
    return "\n".join(report) + "\n", failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    parser.add_argument("--validate", action="store_true", help="Validate configuration offline")
    parser.add_argument("--repo", action="append", help="Limit to an owned repo; repeatable")
    parser.add_argument("--report", type=Path, help="Save the full preview/application report")
    args = parser.parse_args()
    baseline, config = load_configuration(ROOT)
    if args.validate:
        if args.apply:
            parser.error("--validate and --apply are mutually exclusive")

        pending = [name for name, repo in config["repositories"].items() if not repo["checks"]]
        print(f"Configuration valid; {len(pending)} repositories pending CI: {', '.join(pending)}")
        return 0

    report, failures = reconcile(baseline, config, args.apply, args.repo)
    print(report, end="")
    if args.report:
        args.report.write_text(report)

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write("```text\n" + report[:900000] + "\n```\n")

    return int(bool(failures))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GitHubError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
