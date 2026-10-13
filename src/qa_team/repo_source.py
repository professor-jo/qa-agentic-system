"""Turn what the user typed (a Git URL, a GitHub PR URL, or a local path) into a
local checkout on the branch to be tested."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PR_URL = re.compile(r"^(?P<repo>https?://github\.com/[^/]+/[^/]+?)(?:\.git)?/pull/(?P<number>\d+)/?.*$")
REMOTE_URL = re.compile(r"^(https?://|git@|ssh://)")


class RepoError(Exception):
    """Raised when the repository can't be cloned, found or checked out."""


@dataclass
class RepoTarget:
    path: Path
    branch: str      # branch that is checked out and tested; generated PRs target it
    label: str       # human-readable description of what is being tested
    from_pr: bool = False


def _git(repo: Path | None, *args: str) -> subprocess.CompletedProcess:
    # GIT_TERMINAL_PROMPT=0 makes private/missing repos fail fast instead of
    # hanging on a hidden credentials prompt.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env, timeout=600
    )


def _checked(repo: Path | None, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise RepoError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _current_branch(repo: Path) -> str:
    branch = _checked(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise RepoError("The repository is in a detached HEAD state; pass a branch to test.")
    return branch


def _clone(url: str, workspace: Path) -> Path:
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    dest = workspace / name
    if dest.exists():
        # Fresh clone each run so generated tests from a previous run don't leak in.
        shutil.rmtree(dest)
    workspace.mkdir(parents=True, exist_ok=True)
    result = _git(None, "clone", "--quiet", url, str(dest))
    if result.returncode != 0:
        raise RepoError(
            f"Could not clone {url}. Check the URL, and that you have access if it's private.\n"
            f"git said: {result.stderr.strip()}"
        )
    return dest


def resolve(source: str, workspace: Path, branch: str | None = None) -> RepoTarget:
    source = source.strip().strip("'\"")
    if not source:
        raise RepoError("No repository given.")

    if pr := PR_URL.match(source):
        repo = _clone(pr["repo"], workspace)
        number = pr["number"]
        if _git(repo, "fetch", "--quiet", "origin", f"pull/{number}/head:pr-{number}").returncode != 0:
            raise RepoError(f"Pull request #{number} was not found in {pr['repo']}.")
        _checked(repo, "checkout", "--quiet", f"pr-{number}")
        return RepoTarget(repo, f"pr-{number}", f"PR #{number} of {pr['repo']}", from_pr=True)

    if REMOTE_URL.match(source):
        repo = _clone(source, workspace)
        label = source
    else:
        repo = Path(source).expanduser().resolve()
        if not repo.is_dir():
            raise RepoError(f"'{source}' is neither a Git URL nor an existing directory.")
        if _git(repo, "rev-parse", "--git-dir").returncode != 0:
            raise RepoError(f"{repo} is not a Git repository.")
        label = str(repo)

    if branch:
        if _git(repo, "checkout", "--quiet", branch).returncode != 0:
            raise RepoError(f"Branch '{branch}' does not exist in {label}.")
        label += f" @ {branch}"
    current = _current_branch(repo)
    return RepoTarget(repo, current, f"{label} @ {current}" if not branch else label)
