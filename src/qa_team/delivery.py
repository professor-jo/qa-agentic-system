"""Put the QA work on a new branch and open a pull request.

Only what the team produced is committed: the source files the Optimizer
changed, `agentic test/` and `Agentic QA Report/`. The PR targets the branch
that was tested. Without write access, the user can fork and PR upstream or
keep the branch locally.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from qa_team.source import git
from qa_team.stack import REPORT_ROOT, TEST_ROOT
from qa_team.ui import Dashboard, choose

# Push with the gh CLI's login so HTTPS clones need no separate git credentials.
GH_CREDENTIALS = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
MAX_BODY_CHARS = 60_000


@dataclass
class Delivery:
    branch: str
    committed: bool
    pr_url: str = ""
    note: str = ""


def _gh(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def _write_access(repo: Path, slug: str) -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, "the GitHub CLI (gh) is not installed"
    r = _gh("api", f"repos/{slug}", "--jq", ".permissions.push", cwd=repo)
    if r.returncode != 0:
        return False, "couldn't check permissions (run `gh auth login`)"
    if r.stdout.strip() != "true":
        user = _gh("api", "user", "--jq", ".login", cwd=repo).stdout.strip() or "your account"
        return False, f"{user} has no write access to {slug}"
    return True, "ok"


def commit(repo: Path, branch: str, base: str, changed: list[str], message: str) -> str | None:
    """Create `branch` with the QA work committed. Returns an error message, or None on success."""
    identity = [] if git(repo, "config", "user.email").stdout.strip() else \
        ["-c", "user.name=Agentic QA", "-c", "user.email=agentic-qa@users.noreply.github.com"]
    paths = [p for p in [*changed, TEST_ROOT, REPORT_ROOT] if (repo / p).exists()]
    steps = [
        ["checkout", "--quiet", "-b", branch],
        ["add", "--", *paths],
        [*identity, "commit", "--quiet", "-m", message],
    ]
    for args in steps:
        r = git(repo, *args)
        if r.returncode != 0:
            return f"git {args[0] if args[0] != '-c' else 'commit'} failed: {r.stderr.strip()[:300]}"
    return None


def _push(repo: Path, remote: str, branch: str) -> str | None:
    r = git(repo, *GH_CREDENTIALS, "push", "--quiet", "-u", remote, branch, timeout=300)
    return None if r.returncode == 0 else r.stderr.strip()[:300]


def _create_pr(repo: Path, slug: str, base: str, head: str, title: str, body: str) -> tuple[str, str]:
    r = _gh("pr", "create", "--repo", slug, "--base", base, "--head", head, "--title", title, "--body", body, cwd=repo)
    if r.returncode != 0:
        return "", r.stderr.strip()[:300]
    return r.stdout.strip().splitlines()[-1], ""


def _fork_and_pr(repo: Path, slug: str, base: str, branch: str, title: str, body: str) -> tuple[str, str]:
    r = _gh("repo", "fork", slug, "--clone=false", "--remote=false", cwd=repo)
    match = re.search(r"github\.com/([^/\s]+/[^/\s]+)", r.stdout + r.stderr)
    me = _gh("api", "user", "--jq", ".login", cwd=repo).stdout.strip()
    fork = match.group(1) if match else f"{me}/{slug.split('/')[1]}"
    if error := _push(repo, f"https://github.com/{fork}.git", branch):
        return "", f"pushing to your fork {fork} failed: {error}"
    return _create_pr(repo, slug, base, f"{fork.split('/')[0]}:{branch}", title, body)


def deliver(repo: Path, slug: str | None, base: str, branch: str, changed: list[str], passed: bool,
            summary: str, body: str, dashboard: Dashboard, log, open_pr: bool = True) -> Delivery:
    title = f"Agentic QA: fixes, tests and report ({summary})"
    message = f"Agentic QA: fixes, tests and report\n\n{summary}"
    try:
        if error := commit(repo, branch, base, changed, message):
            return Delivery(branch, False, note=error)
        if not open_pr:
            return Delivery(branch, True, note="committed locally (--no-pr)")
        if not slug:
            return Delivery(branch, True, note="committed locally; origin is not a GitHub repo, so no PR")

        dashboard.activity("Delivery", f"checking write access to {slug}")
        writable, reason = _write_access(repo, slug)
        body = body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS] + "\n\n… (truncated)"
        if writable:
            dashboard.activity("Delivery", f"pushing {branch}")
            if error := _push(repo, "origin", branch):
                return Delivery(branch, True, note=f"committed locally, but the push failed: {error}")
            dashboard.activity("Delivery", "opening the pull request")
            url, error = _create_pr(repo, slug, base, branch, title, body)
            return Delivery(branch, True, url, note="" if url else f"pushed, but creating the PR failed: {error}")

        choice = choose(
            f"Can't open a PR directly: {reason}.",
            [("fork", f"Fork {slug} to your account and open a PR to the original repo"),
             ("local", "Keep the branch locally only")],
            default="local",
            suggestions=["If this is your repo under another account, run `gh auth switch` and re-run."],
            dashboard=dashboard, log=log,
        )
        if choice == "fork":
            dashboard.activity("Delivery", "forking and opening a PR")
            url, error = _fork_and_pr(repo, slug, base, branch, title, body)
            return Delivery(branch, True, url, note="" if url else error)
        return Delivery(branch, True, note=f"committed locally ({reason})")
    finally:
        # Leave the checkout on the branch the user tested; the work stays on `branch`.
        if git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch:
            git(repo, "checkout", "--quiet", base)
            remove_leftovers(repo)


def remove_leftovers(repo: Path) -> None:
    """After switching back, only git-ignored tooling (node_modules) remains under
    `agentic test/`. Remove it unless that folder is tracked on this branch."""
    test_root = repo / TEST_ROOT
    if test_root.exists() and not git(repo, "ls-files", "--", TEST_ROOT).stdout.strip():
        leftovers = [p for p in test_root.rglob("*") if p.is_file() and "node_modules" not in p.parts]
        if not leftovers:
            shutil.rmtree(test_root)
