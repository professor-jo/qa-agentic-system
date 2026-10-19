"""Tools the agents use on the target repository.

Every tool shares a `Session`, which knows the repo, its components, which agent
is active (for the live "currently doing" line), and which files were changed.
Permissions are per tool: the Optimizer edits source code, the Test Engineer
writes only inside `agentic test/`, and nobody touches `.git`, dependencies,
lockfiles or the report folder.
"""

import fnmatch
import functools
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from qa_team.stack import HARNESS_FILES, IGNORED_DIRS, REPORT_ROOT, TEST_ROOT, Component, run_component_tests

MAX_READ_CHARS = 40_000
MAX_OUTPUT_CHARS = 16_000
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Pipfile.lock"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".woff", ".woff2", ".ttf", ".mp4", ".mp3"}


@dataclass
class Session:
    repo: Path
    components: list[Component]
    on_activity: Callable[[str, str], None]
    agent: str = ""
    source_changes: dict[str, str] = field(default_factory=dict)   # path -> "edited" | "created"
    originals: dict[str, str | None] = field(default_factory=dict)  # content before the first edit (None = new file)
    test_files: set[str] = field(default_factory=set)
    test_runs: int = 0
    usage: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "cached": 0, "completion": 0})
    task_steps: int = 0
    task_limit: int = 0
    read_cache: dict = field(default_factory=dict)

    def begin_task(self, limit: int) -> None:
        """Each agent task gets a visible step budget and a fresh read cache."""
        self.task_steps, self.task_limit = 0, limit
        self.read_cache.clear()

    def step_note(self) -> str:
        """Appended to every tool result, so the agent sees its budget shrinking."""
        self.task_steps += 1
        if not self.task_limit:
            return ""
        left = self.task_limit - self.task_steps
        if left <= 0:
            return "\n[step budget used up: stop using tools and give your final answer now]"
        if left <= 3:
            return f"\n[step {self.task_steps}/{self.task_limit}: only {left} left, wrap up and give your final answer]"
        return f"\n[step {self.task_steps}/{self.task_limit}]"

    def activity(self, what: str) -> None:
        self.on_activity(self.agent or "agent", what)

    def resolve(self, relative: str) -> Path:
        path = (self.repo / relative.strip().lstrip("/")).resolve()
        if path != self.repo and self.repo not in path.parents:
            raise ValueError(f"'{relative}' is outside the repository.")
        return path

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.repo))

    def snapshot(self, file: Path) -> None:
        self.originals.setdefault(self.rel(file), file.read_text() if file.exists() else None)

    def restore_sources(self) -> None:
        """Undo every source edit/creation made by the agents (exact original contents)."""
        for rel, original in self.originals.items():
            file = self.repo / rel
            if original is None:
                file.unlink(missing_ok=True)
            else:
                file.write_text(original)
        self.originals.clear()
        self.source_changes.clear()


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


class SessionTool(BaseTool):
    session: Any = Field(..., exclude=True)

    def __init_subclass__(cls, **kwargs):
        """Wrap every tool's _run so its result ends with the step-budget note."""
        super().__init_subclass__(**kwargs)
        if "_run" in cls.__dict__:
            original = cls.__dict__["_run"]

            @functools.wraps(original)
            def counted(self, *args, **kw):
                return f"{original(self, *args, **kw)}{self.session.step_note()}"
            cls._run = counted


# --- read-only ------------------------------------------------------------------

class ListFilesInput(BaseModel):
    subdirectory: str = Field(default=".", description="Directory to list, relative to the repo root.")


class ListFilesTool(SessionTool):
    name: str = "list_files"
    description: str = "Recursively list files in the repository or a subdirectory (dependencies, caches and .git are skipped)."
    args_schema: Type[BaseModel] = ListFilesInput

    def _run(self, subdirectory: str = ".") -> str:
        s: Session = self.session
        try:
            start = s.resolve(subdirectory)
        except ValueError as e:
            return str(e)
        s.activity(f"listing {subdirectory if subdirectory not in ('', '.') else 'the repository'}")
        files = []
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
            files += [s.rel(Path(dirpath) / f) for f in sorted(filenames)]
        return _truncate("\n".join(files) or "(no files)")


class ReadFileInput(BaseModel):
    path: str = Field(..., description="File path relative to the repo root.")
    start_line: int = Field(default=1, description="First line to read (1-based).")
    end_line: int = Field(default=0, description="Last line to read; 0 means to the end.")


class ReadFileTool(SessionTool):
    name: str = "read_file"
    description: str = "Read a text file with line numbers. Use start_line/end_line for large files."
    args_schema: Type[BaseModel] = ReadFileInput

    def _run(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        s: Session = self.session
        try:
            file = s.resolve(path)
            if file.suffix.lower() in BINARY_SUFFIXES:
                return f"'{path}' is a binary file."
            lines = file.read_text(errors="replace").splitlines()
        except (ValueError, OSError) as e:
            return f"Error reading '{path}': {e}"
        key = (s.rel(file), start_line, end_line)
        stamp = file.stat().st_mtime_ns
        if s.read_cache.get(key) == stamp:
            return (f"You already read {path} (same lines) earlier in this task and it hasn't changed. "
                    "Use what you have instead of reading it again.")
        s.read_cache[key] = stamp
        s.activity(f"reading {path}")
        end = end_line or len(lines)
        numbered = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(max(start_line, 1), min(end, len(lines)) + 1))
        return _truncate(numbered or "(empty file)", MAX_READ_CHARS)


class SearchInput(BaseModel):
    pattern: str = Field(..., description="Regular expression to search for.")
    glob: str = Field(default="", description="Optional filename filter, e.g. '*.py' or '*.jsx'.")


class SearchCodeTool(SessionTool):
    name: str = "search_code"
    description: str = "Search the repository's files for a regular expression. Returns path:line matches."
    args_schema: Type[BaseModel] = SearchInput

    def _run(self, pattern: str, glob: str = "") -> str:
        s: Session = self.session
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Invalid regex: {e}"
        s.activity(f"searching for '{pattern[:40]}'")
        hits = []
        for dirpath, dirnames, filenames in os.walk(s.repo):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for name in filenames:
                if (glob and not fnmatch.fnmatch(name, glob)) or Path(name).suffix.lower() in BINARY_SUFFIXES:
                    continue
                file = Path(dirpath) / name
                try:
                    for i, line in enumerate(file.read_text(errors="replace").splitlines(), start=1):
                        if regex.search(line):
                            hits.append(f"{s.rel(file)}:{i}: {line.strip()[:200]}")
                except OSError:
                    continue
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n... (first 200 matches)"
        return "\n".join(hits) or "(no matches)"


# --- Optimizer: source edits ------------------------------------------------------

def _source_edit_refusal(s: Session, file: Path) -> str | None:
    rel = s.rel(file)
    parts = Path(rel).parts
    if parts and parts[0] in (TEST_ROOT, REPORT_ROOT):
        return f"Refused: '{rel}' belongs to the QA output folders; source edits only."
    if any(p in IGNORED_DIRS or p.startswith(".") for p in parts[:-1]) or file.name.startswith(".env"):
        return f"Refused: '{rel}' is in a protected location (dependencies, caches, .git, env files)."
    if file.name in LOCKFILES:
        return f"Refused: '{rel}' is a lockfile. Don't change dependencies; suggest upgrades in your report instead."
    return None


class EditFileInput(BaseModel):
    path: str = Field(..., description="Source file path relative to the repo root.")
    old_text: str = Field(..., description="Exact text to replace. Must match the file exactly and appear once.")
    new_text: str = Field(..., description="Replacement text.")


class EditFileTool(SessionTool):
    name: str = "edit_file"
    description: str = (
        "Replace an exact, unique snippet in a source file. Read the file first and copy old_text exactly, "
        "including indentation. Include enough surrounding lines to make it unique."
    )
    args_schema: Type[BaseModel] = EditFileInput

    def _run(self, path: str, old_text: str, new_text: str) -> str:
        s: Session = self.session
        try:
            file = s.resolve(path)
        except ValueError as e:
            return str(e)
        if refusal := _source_edit_refusal(s, file):
            return refusal
        if not file.is_file():
            return f"'{path}' does not exist. Use create_file for new files."
        text = file.read_text()
        count = text.count(old_text)
        if count != 1:
            return f"old_text was found {count} times in {path}; it must match exactly once. Re-read the file and retry."
        s.activity(f"editing {path}")
        s.snapshot(file)
        file.write_text(text.replace(old_text, new_text, 1))
        s.source_changes.setdefault(s.rel(file), "edited")
        return f"Edited {path}."


class CreateFileInput(BaseModel):
    path: str = Field(..., description="New source file path relative to the repo root.")
    content: str = Field(..., description="Full file contents.")


class CreateFileTool(SessionTool):
    name: str = "create_file"
    description: str = "Create a NEW source file (e.g. when extracting a module). Existing files must be changed with edit_file."
    args_schema: Type[BaseModel] = CreateFileInput

    def _run(self, path: str, content: str) -> str:
        s: Session = self.session
        try:
            file = s.resolve(path)
        except ValueError as e:
            return str(e)
        if refusal := _source_edit_refusal(s, file):
            return refusal
        if file.exists() and s.source_changes.get(s.rel(file)) != "created":
            return f"'{path}' already exists. Use edit_file to change it."
        s.activity(f"creating {path}")
        s.snapshot(file)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        s.source_changes[s.rel(file)] = "created"
        return f"Created {path}."


# --- Test Engineer: agentic test/ only --------------------------------------------

class WriteTestInput(BaseModel):
    path: str = Field(..., description=f"Path inside '{TEST_ROOT}/<component>/', e.g. '{TEST_ROOT}/backend/unit/test_auth.py'.")
    content: str = Field(..., description="Full file contents.")


class WriteTestFileTool(SessionTool):
    name: str = "write_test_file"
    description: str = (
        f"Create or overwrite a test file. Only paths inside '{TEST_ROOT}/<component>/' are allowed, "
        "and the generated harness files (package.json, vitest.config.mjs, setup.mjs) can't be changed."
    )
    args_schema: Type[BaseModel] = WriteTestInput

    def _run(self, path: str, content: str) -> str:
        s: Session = self.session
        try:
            file = s.resolve(path)
        except ValueError as e:
            return str(e)
        parts = Path(s.rel(file)).parts
        names = {c.name for c in s.components}
        if len(parts) < 3 or parts[0] != TEST_ROOT or parts[1] not in names:
            return f"Refused: tests must go in '{TEST_ROOT}/<component>/...' where component is one of: {', '.join(sorted(names))}."
        if len(parts) == 3 and parts[2] in HARNESS_FILES or "node_modules" in parts:
            return f"Refused: '{s.rel(file)}' is part of the generated test harness."
        s.activity(f"writing {s.rel(file)}")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        s.test_files.add(s.rel(file))
        return f"Wrote {s.rel(file)}."


class RunTestsInput(BaseModel):
    component: str = Field(default="", description="Component name to run; empty runs all components.")
    target: str = Field(default="", description="Optional single test file (repo-relative path) within that component.")


class RunTestsTool(SessionTool):
    name: str = "run_tests"
    description: str = "Run the tests in 'agentic test/' (all components, one component, or one file) and return the output."
    args_schema: Type[BaseModel] = RunTestsInput

    def _run(self, component: str = "", target: str = "") -> str:
        s: Session = self.session
        chosen = [c for c in s.components if not component or c.name == component]
        if not chosen:
            return f"Unknown component '{component}'. Choose from: {', '.join(c.name for c in s.components)}."
        s.test_runs += 1
        out = []
        for comp in chosen:
            s.activity(f"running {comp.name} tests (run #{s.test_runs})")
            result = run_component_tests(s.repo, comp, target if component else "")
            if not result.has_tests:
                out.append(f"## {comp.name}: NO TESTS YET. Write tests in '{comp.tests_dir}/' for this component.")
                continue
            out.append(f"## {comp.name}: {'PASS' if result.ok else 'FAIL'} ({result.summary})\n{_truncate(result.output, MAX_OUTPUT_CHARS // len(chosen))}")
        return "\n\n".join(out)


def read_tools(session: Session) -> list[BaseTool]:
    return [ListFilesTool(session=session), ReadFileTool(session=session), SearchCodeTool(session=session)]


def optimizer_tools(session: Session) -> list[BaseTool]:
    return [*read_tools(session), EditFileTool(session=session), CreateFileTool(session=session)]


def tester_tools(session: Session) -> list[BaseTool]:
    return [*read_tools(session), WriteTestFileTool(session=session), RunTestsTool(session=session)]
