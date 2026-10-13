"""Prepare a target repository for testing: find where its Python project and
tests live, and build an isolated virtualenv with its dependencies installed."""

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from qa_team.tools.repo_tools import IGNORED_DIRS

PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
REQUIREMENT_FILES = (
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "requirements_dev.txt", "requirements_test.txt", "dev-requirements.txt",
    "test-requirements.txt", "requirements/dev.txt", "requirements/test.txt",
)
TEST_GROUPS = ("test", "tests", "testing", "dev")
TEST_DIR_NAMES = ("tests", "test")
INSTALL_TIMEOUT = 900


class EnvError(Exception):
    """Raised when the repository's environment can't be built."""


@dataclass
class RepoEnv:
    python: str        # interpreter that has the repo's dependencies
    project_dir: str   # directory pytest runs from, relative to the repo root ("." for root)
    tests_dir: str     # where tests live / get written, relative to the repo root
    log: list[str] = field(default_factory=list)


def _walk_dirs(root: Path, max_depth: int):
    for dirpath, dirnames, _ in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        yield Path(dirpath)


def _is_project(directory: Path) -> bool:
    return any((directory / m).exists() for m in PROJECT_MARKERS)


def _pytest_testpaths(project: Path) -> list[str]:
    pyproject = project / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        data = tomllib.loads(pyproject.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return []
    paths = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", [])
    return [paths] if isinstance(paths, str) else list(paths)


def detect_layout(repo: Path, tests_dir_override: str | None = None) -> tuple[Path, Path]:
    """Return (project_dir, tests_dir) as absolute paths."""
    if tests_dir_override:
        tests = (repo / tests_dir_override).resolve()
        project = next((p for p in [tests.parent, *tests.parents] if _is_project(p) and repo in [p, *p.parents]), repo)
        return project, tests

    projects = [d for d in _walk_dirs(repo, max_depth=2) if _is_project(d)]

    # 1. pytest's own configuration is authoritative.
    for project in projects:
        for testpath in _pytest_testpaths(project):
            if (project / testpath).is_dir():
                return project, project / testpath

    # 2. An existing tests/ directory, run from the nearest project above it.
    for directory in _walk_dirs(repo, max_depth=3):
        if directory.name in TEST_DIR_NAMES and directory != repo:
            project = next((p for p in directory.parents if _is_project(p) and repo in [p, *p.parents]), repo)
            return project, directory

    # 3. No tests yet: put them next to the shallowest Python project.
    project = projects[0] if projects else repo
    return project, project / "tests"


def _run(cmd: list[str], cwd: Path, log: list[str]) -> subprocess.CompletedProcess:
    log.append(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise EnvError(f"Timed out after {INSTALL_TIMEOUT}s: {' '.join(cmd)}")
    if result.returncode != 0:
        log.append(result.stderr.strip()[-2000:])
    return result


def _declared_test_deps(project: Path) -> tuple[list[str], list[str]]:
    """Dependency groups and extras in pyproject.toml that look test-related."""
    pyproject = project / "pyproject.toml"
    if not pyproject.exists():
        return [], []
    try:
        data = tomllib.loads(pyproject.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return [], []
    groups = [g for g in TEST_GROUPS if g in data.get("dependency-groups", {})]
    extras = [e for e in TEST_GROUPS if e in data.get("project", {}).get("optional-dependencies", {})]
    return groups, extras


def _has_python_code(repo: Path) -> bool:
    return any(any(directory.glob("*.py")) or _is_project(directory) for directory in _walk_dirs(repo, max_depth=4))


def build_env(repo: Path, venv_dir: Path, tests_dir_override: str | None = None) -> RepoEnv:
    if not _has_python_code(repo):
        raise EnvError("No Python code found. The QA team currently supports Python repositories only (pytest).")
    project, tests = detect_layout(repo, tests_dir_override)
    log: list[str] = []
    rel = lambda p: str(p.relative_to(repo)) if p != repo else "."  # noqa: E731

    uv = shutil.which("uv")
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)

    # uv honours the project's .python-version / requires-python when picking an interpreter.
    create = [uv, "venv", "--quiet", str(venv_dir)] if uv else [sys.executable, "-m", "venv", str(venv_dir)]
    if _run(create, project, log).returncode != 0:
        raise EnvError("Could not create a virtualenv.\n" + log[-1])
    python = str(venv_dir / "bin" / "python")
    pip = [uv, "pip", "install", "--quiet", "--python", python] if uv else [python, "-m", "pip", "install", "--quiet"]

    if (project / "pyproject.toml").exists() or (project / "setup.py").exists():
        groups, extras = _declared_test_deps(project)
        target = f".[{','.join(extras)}]" if extras else "."
        cmd = [*pip, "-e", target]
        if uv:
            for group in groups:
                cmd += ["--group", group]
        if _run(cmd, project, log).returncode != 0:
            raise EnvError(f"Installing the project in {rel(project)}/ failed.\n" + log[-1])

    for name in REQUIREMENT_FILES:
        if (project / name).exists():
            if _run([*pip, "-r", name], project, log).returncode != 0:
                raise EnvError(f"Installing {rel(project)}/{name} failed.\n" + log[-1])

    if _run([*pip, "pytest"], project, log).returncode != 0:
        raise EnvError("Installing pytest failed.\n" + log[-1])

    return RepoEnv(python=python, project_dir=rel(project), tests_dir=rel(tests), log=log)
