"""Turn user input (GitHub URL, local path, or a pick from your GitHub) into a
local Git checkout to work on."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REMOTE_URL = re.compile(r"^(https?://|git@|ssh://)")
GITHUB_SLUG = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


class SourceError(Exception):
    """The input isn't an accessible repository. The message is shown to the user as-is."""


@dataclass
class Target:
    path: Path
    branch: str           # branch being QA'd; the PR targets it
    label: str
    is_clone: bool        # True when we cloned it into the workspace
    slug: str | None      # owner/repo on GitHub, if origin is GitHub


def git(repo: Path | None, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    # GIT_TERMINAL_PROMPT=0: private/missing repos fail fast instead of hanging on a hidden prompt.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, timeout=timeout)


def github_slug(repo: Path) -> str | None:
    remote = git(repo, "remote", "get-url", "origin")
    match = GITHUB_SLUG.search(remote.stdout.strip()) if remote.returncode == 0 else None
    return f"{match['owner']}/{match['repo']}" if match else None


def current_branch(repo: Path) -> str:
    result = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0 or result.stdout.strip() == "HEAD":
        raise SourceError(f"{repo} is in a detached HEAD state. Check out a branch first.")
    return result.stdout.strip()


def unpushed_qa_branches(repo: Path) -> list[str]:
    """Local qa/* branches with no matching remote branch (work that would be lost)."""
    local = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/qa/").stdout.split()
    remote = set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/qa/").stdout.split())
    return [b for b in local if f"origin/{b}" not in remote]


def clone_dir_for(url: str, workspace: Path) -> Path:
    name = url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return workspace / name


def clone(url: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = git(None, "clone", "--quiet", url, str(dest))
    if result.returncode != 0:
        raise SourceError(
            f"{url} is not an accessible repository. It may not exist, or it may be private "
            f"and your account has no access.\n(git: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'clone failed'})"
        )


def keep_old_clone(dest: Path) -> Path:
    """Move an old clone aside instead of deleting it."""
    moved = dest.with_name(f"{dest.name}-{datetime.now():%Y%m%d-%H%M%S}")
    dest.rename(moved)
    return moved


def clean_path(text: str) -> str:
    """Undo what dragging a folder into the terminal adds: quotes, '\\ ' escapes, trailing space."""
    text = text.strip().strip("'\"")
    return re.sub(r"\\(.)", r"\1", text)


def open_local(path_text: str) -> Path:
    path = Path(clean_path(path_text)).expanduser().resolve()
    if not path.is_dir():
        raise SourceError(f"'{path_text}' is not an accessible repository: the folder doesn't exist.")
    if git(path, "rev-parse", "--git-dir").returncode != 0:
        raise SourceError(
            f"{path} is not an accessible repository: it isn't a Git repo. "
            "Run `git init` and commit your code first, so changes can go on a branch."
        )
    top = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip())
    return top


def is_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain").stdout.strip())


def checkout(repo: Path, branch: str) -> None:
    if git(repo, "checkout", "--quiet", branch).returncode != 0:
        raise SourceError(f"Branch '{branch}' does not exist in {repo.name}.")


# --- your GitHub -----------------------------------------------------------

def list_my_repos(limit: int = 200) -> list[dict]:
    if not shutil.which("gh"):
        raise SourceError("The GitHub CLI (gh) is not installed, so your repositories can't be listed. "
                          "Install it with `brew install gh` and run `gh auth login`.")
    result = subprocess.run(
        ["gh", "repo", "list", "--limit", str(limit), "--no-archived",
         "--json", "nameWithOwner,url,primaryLanguage,updatedAt,isFork"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise SourceError("Couldn't list your GitHub repositories. Run `gh auth login` and try again.")
    repos = json.loads(result.stdout)
    return sorted(repos, key=lambda r: r["updatedAt"], reverse=True)


def parse_selection(text: str, count: int) -> list[int]:
    """'1,3-5' or 'all' -> zero-based indexes."""
    text = text.strip().lower()
    if text == "all":
        return list(range(count))
    picked: list[int] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        if "-" in part:
            start, end = part.split("-", 1)
            picked += range(int(start) - 1, int(end))
        else:
            picked.append(int(part) - 1)
    if any(i < 0 or i >= count for i in picked):
        raise ValueError(f"choose numbers between 1 and {count}")
    return list(dict.fromkeys(picked))
