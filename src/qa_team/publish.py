"""Step 4: if the user can push to the GitHub repo, commit the generated tests
to a new branch, push it, and open a pull request with the reports."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
MAX_BODY_CHARS = 60_000  # GitHub's limit is 65,536
# Use the gh CLI's login for the push, so HTTPS clones don't need separate git credentials.
GH_CREDENTIALS = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


@dataclass
class PublishResult:
    created: bool
    message: str  # PR URL on success, otherwise why it was skipped or failed


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)


def _github_repo(repo: Path) -> str | None:
    remote = _run(["git", "remote", "get-url", "origin"], repo)
    match = GITHUB_REMOTE.search(remote.stdout.strip()) if remote.returncode == 0 else None
    return f"{match['owner']}/{match['repo']}" if match else None


def check_write_access(repo: Path) -> tuple[str | None, str]:
    """Return (owner/repo, reason). owner/repo is None when we can't or may not push."""
    slug = _github_repo(repo)
    if not slug:
        return None, "origin is not a GitHub repository"
    if not shutil.which("gh"):
        return None, "the GitHub CLI (gh) is not installed"
    result = _run(["gh", "api", f"repos/{slug}", "--jq", ".permissions.push"], repo)
    if result.returncode != 0:
        return None, "could not check permissions (is `gh auth login` done?)"
    if result.stdout.strip() != "true":
        user = _run(["gh", "api", "user", "--jq", ".login"], repo).stdout.strip() or "your account"
        return None, f"{user} has no write access to {slug}"
    return slug, "ok"


def _pr_body(test_summary: str, reports: dict[str, Path], files: list[str]) -> str:
    sections = [
        "Automated QA run by the CrewAI QA team.",
        f"**Test result:** {test_summary}",
        "**New test files:**\n" + "\n".join(f"- `{f}`" for f in files),
    ]
    for title, name in (("Test report", "TEST_REPORT.md"), ("Bugs & optimizations", "FINDINGS.md")):
        if name in reports:
            sections.append(f"<details><summary>{title}</summary>\n\n{reports[name].read_text()}\n</details>")
    body = "\n\n".join(sections)
    return body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS] + "\n\n… (truncated, see the full reports locally)"


def publish(repo: Path, base_branch: str, files: list[str], test_summary: str,
            tests_passed: bool, reports: dict[str, Path]) -> PublishResult:
    if not files:
        return PublishResult(False, "skipped: the crew created no test files")
    slug, reason = check_write_access(repo)
    if not slug:
        return PublishResult(False, f"skipped: {reason}")

    branch = f"qa/tests-{datetime.now():%Y%m%d-%H%M%S}"
    identity = []
    if not _run(["git", "config", "user.email"], repo).stdout.strip():
        identity = ["-c", "user.name=QA Team", "-c", "user.email=qa-team@users.noreply.github.com"]

    steps = [
        ["git", "checkout", "--quiet", "-b", branch],
        ["git", "add", "--", *files],
        ["git", *identity, "commit", "--quiet", "-m", f"Add QA test suite ({test_summary})"],
        ["git", *GH_CREDENTIALS, "push", "--quiet", "-u", "origin", branch],
    ]
    try:
        for cmd in steps:
            result = _run(cmd, repo)
            if result.returncode != 0:
                return PublishResult(False, f"failed at `{' '.join(cmd[:3])}`: {result.stderr.strip()[:300]}")

        title = "QA: add test suite" + ("" if tests_passed else " (failing tests reproduce bugs)")
        result = _run(["gh", "pr", "create", "--repo", slug, "--base", base_branch, "--head", branch,
                       "--title", title, "--body", _pr_body(test_summary, reports, files)], repo)
        if result.returncode != 0:
            return PublishResult(False, f"pushed {branch}, but creating the PR failed: {result.stderr.strip()[:300]}")
        return PublishResult(True, result.stdout.strip().splitlines()[-1])
    finally:
        # Leave the checkout on the branch the user tested.
        _run(["git", "checkout", "--quiet", base_branch], repo)
