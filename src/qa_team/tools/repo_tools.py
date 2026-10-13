"""Tools that give the QA agents scoped access to a target Git repository.

Every tool is bound to a single repo root. Reads can touch any file inside it;
writes are restricted to the configured tests directory so agents can't modify
the code under test.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

IGNORED_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode", ".qa-venv",
}
MAX_READ_CHARS = 40_000
MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [truncated {len(text) - limit} characters]"


def _resolve_inside(root: Path, relative: str) -> Path:
    """Resolve `relative` against `root`, refusing paths that escape it."""
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"Path '{relative}' is outside the repository.")
    return path


class RepoTool(BaseTool):
    repo_root: str = Field(..., description="Absolute path to the target repository.")

    @property
    def root(self) -> Path:
        return Path(self.repo_root).resolve()


class ListFilesInput(BaseModel):
    subdirectory: str = Field(
        default=".", description="Directory to list, relative to the repo root."
    )


class ListFilesTool(RepoTool):
    name: str = "list_repo_files"
    description: str = (
        "Recursively list files in the repository (or a subdirectory), skipping "
        "virtualenvs, caches and .git. Use this to understand the project layout."
    )
    args_schema: Type[BaseModel] = ListFilesInput

    def _run(self, subdirectory: str = ".") -> str:
        try:
            start = _resolve_inside(self.root, subdirectory)
        except ValueError as e:
            return str(e)
        files = []
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for name in sorted(filenames):
                files.append(str((Path(dirpath) / name).relative_to(self.root)))
        return _truncate("\n".join(files) or "(no files)")


class ReadFileInput(BaseModel):
    path: str = Field(..., description="File path relative to the repo root.")


class ReadFileTool(RepoTool):
    name: str = "read_repo_file"
    description: str = "Read a text file from the repository, with line numbers."
    args_schema: Type[BaseModel] = ReadFileInput

    def _run(self, path: str) -> str:
        try:
            file = _resolve_inside(self.root, path)
            text = file.read_text(errors="replace")
        except (ValueError, OSError) as e:
            return f"Error reading '{path}': {e}"
        numbered = "\n".join(
            f"{i:>5}  {line}" for i, line in enumerate(text.splitlines(), start=1)
        )
        return _truncate(numbered, MAX_READ_CHARS)


class WriteTestFileInput(BaseModel):
    path: str = Field(
        ..., description="Test file path relative to the repo root, e.g. 'tests/test_utils.py'."
    )
    content: str = Field(..., description="Full file contents to write.")


class WriteTestFileTool(RepoTool):
    name: str = "write_test_file"
    description: str = (
        "Create a new test file, or rewrite one you created earlier in this run. "
        "Only paths inside the tests directory are allowed. Existing project files, "
        "including existing tests and conftest.py, cannot be modified."
    )
    args_schema: Type[BaseModel] = WriteTestFileInput
    tests_dir: str = Field(default="tests")
    written: set[str] = Field(default_factory=set, description="Repo-relative paths created by this tool.")

    def _run(self, path: str, content: str) -> str:
        try:
            tests_root = _resolve_inside(self.root, self.tests_dir)
            file = _resolve_inside(self.root, path)
        except ValueError as e:
            return str(e)
        if tests_root not in file.parents:
            return f"Refused: '{path}' is not inside '{self.tests_dir}/'."
        relative = str(file.relative_to(self.root))
        if file.exists() and relative not in self.written:
            return f"Refused: '{relative}' already exists in the project. Create a new test file instead."
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        self.written.add(relative)
        return f"Wrote {len(content)} characters to {relative}"


class RunTestsInput(BaseModel):
    target: str = Field(
        default="",
        description="Optional pytest target (file, dir, or node id). Empty runs the tests directory.",
    )


class RunTestsTool(RepoTool):
    name: str = "run_tests"
    description: str = (
        "Run pytest with the repository's dependencies installed and return the output. "
        "Pass a target path relative to the repo root, or leave it empty to run the tests directory."
    )
    args_schema: Type[BaseModel] = RunTestsInput
    tests_dir: str = Field(default="tests")
    project_dir: str = Field(default=".", description="Directory pytest runs from, relative to the repo root.")
    python: str = Field(default=sys.executable, description="Interpreter with the repo's dependencies.")
    timeout_seconds: int = Field(default=300)

    def _run(self, target: str = "") -> str:
        try:
            cwd = _resolve_inside(self.root, self.project_dir)
            # Agents speak in repo-relative paths; pytest runs from the project dir.
            path_part, sep, node = (target or self.tests_dir).partition("::")
            abs_target = _resolve_inside(self.root, path_part)
        except ValueError as e:
            return str(e)
        rel_target = os.path.relpath(abs_target, cwd) + sep + node
        cmd = [self.python, "-m", "pytest", rel_target, "-q", "--tb=short", "-p", "no:cacheprovider"]
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=self.timeout_seconds
            )
        except subprocess.TimeoutExpired:
            return f"pytest timed out after {self.timeout_seconds}s."
        output = (result.stdout + "\n" + result.stderr).strip()
        where = "" if self.project_dir == "." else f"(cd {self.project_dir}) "
        return _truncate(f"$ {where}pytest {' '.join(cmd[3:])}\nexit code: {result.returncode}\n\n{output}")
