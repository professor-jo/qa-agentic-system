"""Detect the Python and JS/TS parts of a repository, install their dependencies,
scaffold the `agentic test/` harness, and run tests.

Everything the testing step creates lives under `agentic test/`. Virtualenvs live
outside the repo (in the workspace), and JS test tooling is installed into
`agentic test/<component>/node_modules` (git-ignored there), so the project's own
package.json and lockfile are never touched.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TEST_ROOT = "agentic test"
REPORT_ROOT = "Agentic QA Report"
IGNORED_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", ".astro", ".svelte-kit", ".turbo",
    ".idea", ".vscode", "coverage", ".cache", TEST_ROOT, REPORT_ROOT,
}
PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
REQUIREMENT_FILES = (
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements_dev.txt",
    "requirements_test.txt", "dev-requirements.txt", "test-requirements.txt",
    "requirements/dev.txt", "requirements/test.txt",
)
TEST_GROUPS = ("test", "tests", "testing", "dev")
DOM_LIBS = ("react", "react-dom", "vue", "svelte", "preact", "solid-js", "lit")
INSTALL_TIMEOUT = 900
TEST_TIMEOUT = 600


class SetupError(Exception):
    """A component's environment couldn't be built. First line is the summary."""


@dataclass
class Component:
    name: str                    # slug used for `agentic test/<name>/`
    lang: str                    # "python" or "js"
    dir: str                     # relative to repo root, "." for the root
    python: str = ""             # venv interpreter (python components)
    existing_tests: str = ""     # repo-relative path of the project's own tests (python only)
    baseline: str = ""           # result of the existing tests before any change
    baseline_ok: bool = False
    dom: bool = False            # JS component that needs a browser-like environment
    notes: list[str] = field(default_factory=list)

    @property
    def tests_dir(self) -> str:
        return f"{TEST_ROOT}/{self.name}"

    def describe(self) -> str:
        where = "repo root" if self.dir == "." else f"{self.dir}/"
        runner = "pytest" if self.lang == "python" else f"vitest ({'jsdom' if self.dom else 'node'})"
        return f"{self.name}: {self.lang} in {where}, tests in '{self.tests_dir}/' run with {runner}"


@dataclass
class TestRun:
    ok: bool
    summary: str
    output: str
    has_tests: bool = True


def walk_dirs(root: Path, max_depth: int):
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
        yield Path(dirpath), filenames


def _rel(repo: Path, path: Path) -> str:
    return "." if path == repo else str(path.relative_to(repo))


def _slug(lang: str, rel: str) -> str:
    return ("python" if lang == "python" else "javascript") if rel == "." else rel.replace("/", "-")


def _is_js_project(directory: Path) -> bool:
    try:
        data = json.loads((directory / "package.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("dependencies") or data.get("devDependencies") or data.get("scripts") or data.get("main"))


def detect_components(repo: Path) -> list[Component]:
    found: list[Component] = []
    py_dirs: list[Path] = []
    js_dirs: list[Path] = []
    loose_python = False
    for directory, files in walk_dirs(repo, max_depth=3):
        if any(m in files for m in PY_MARKERS) and not any(p in directory.parents for p in py_dirs):
            py_dirs.append(directory)
        if "package.json" in files and _is_js_project(directory) and not any(p in directory.parents for p in js_dirs):
            js_dirs.append(directory)
        loose_python = loose_python or any(f.endswith(".py") for f in files)
    if not py_dirs and loose_python:
        py_dirs.append(repo)  # plain scripts with no packaging
    for d in py_dirs:
        found.append(Component(_slug("python", _rel(repo, d)), "python", _rel(repo, d)))
    for d in js_dirs:
        found.append(Component(_slug("js", _rel(repo, d)), "js", _rel(repo, d)))
    return found


# --- shared helpers ----------------------------------------------------------

def _run(cmd: list[str], cwd: Path, log, timeout: int = INSTALL_TIMEOUT, env: dict | None = None) -> subprocess.CompletedProcess:
    log.write(f"$ (cd {cwd}) {' '.join(cmd)}\n")
    log.flush()
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        raise SetupError(f"Timed out after {timeout}s: {' '.join(cmd)}")
    if result.returncode != 0:
        log.write(result.stdout[-3000:] + "\n" + result.stderr[-3000:] + "\n")
    return result


def _tail(result: subprocess.CompletedProcess, lines: int = 6) -> str:
    text = (result.stderr.strip() or result.stdout.strip()).splitlines()
    return "\n".join(text[-lines:])


# --- Python ------------------------------------------------------------------

def _pyproject(project: Path) -> dict:
    try:
        return tomllib.loads((project / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _existing_python_tests(repo: Path, project: Path) -> str:
    for testpath in _pyproject(project).get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths", []):
        if (project / testpath).is_dir():
            return _rel(repo, project / testpath)
    for name in ("tests", "test"):
        if (project / name).is_dir():
            return _rel(repo, project / name)
    return ""


def setup_python(repo: Path, comp: Component, venv_dir: Path, log) -> None:
    project = repo / comp.dir
    uv = shutil.which("uv")
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    create = [uv, "venv", "--quiet", str(venv_dir)] if uv else [sys.executable, "-m", "venv", str(venv_dir)]
    if (r := _run(create, project, log)).returncode != 0:
        raise SetupError(f"Could not create a Python virtualenv for {comp.name}.\n{_tail(r)}")
    comp.python = str(venv_dir / "bin" / "python")
    pip = [uv, "pip", "install", "--quiet", "--python", comp.python] if uv else [comp.python, "-m", "pip", "install", "--quiet"]

    if (project / "pyproject.toml").exists() or (project / "setup.py").exists():
        data = _pyproject(project)
        groups = [g for g in TEST_GROUPS if g in data.get("dependency-groups", {})]
        extras = [e for e in TEST_GROUPS if e in data.get("project", {}).get("optional-dependencies", {})]
        cmd = [*pip, "-e", f".[{','.join(extras)}]" if extras else "."]
        for group in groups if uv else []:
            cmd += ["--group", group]
        if (r := _run(cmd, project, log)).returncode != 0:
            raise SetupError(f"Installing the Python project in {comp.dir}/ failed.\n{_tail(r)}")
    for name in REQUIREMENT_FILES:
        if (project / name).exists() and (r := _run([*pip, "-r", name], project, log)).returncode != 0:
            raise SetupError(f"Installing {comp.dir}/{name} failed.\n{_tail(r)}")
    if (r := _run([*pip, "pytest"], project, log)).returncode != 0:
        raise SetupError(f"Installing pytest failed.\n{_tail(r)}")

    (repo / comp.tests_dir).mkdir(parents=True, exist_ok=True)
    comp.existing_tests = _existing_python_tests(repo, project)
    if comp.existing_tests:
        run = run_pytest(repo, comp, comp.existing_tests, importlib_mode=False)
        comp.baseline, comp.baseline_ok = run.summary, run.ok
    else:
        comp.baseline = "none"


def _summarize_pytest(output: str) -> str:
    matches = re.findall(r"^=*\s*(\d+ (?:passed|failed|error|errors|skipped).*?) in [\d.]+s", output, re.M)
    if matches:
        return matches[-1]
    return "no tests" if "no tests ran" in output or "not found" in output else "pytest could not run"


def run_pytest(repo: Path, comp: Component, target: str, importlib_mode: bool = True) -> TestRun:
    cwd = repo / comp.dir
    rel_target = os.path.relpath(repo / target, cwd)
    cmd = [comp.python, "-m", "pytest", rel_target, "-q", "--tb=short", "-p", "no:cacheprovider",
           "-o", "addopts="]
    if importlib_mode:
        cmd.append("--import-mode=importlib")
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return TestRun(False, f"timed out after {TEST_TIMEOUT}s", "")
    output = (result.stdout + "\n" + result.stderr).strip()
    summary = _summarize_pytest(output)
    return TestRun(result.returncode == 0, summary, output, has_tests=result.returncode != 5 and summary != "no tests")


# --- JavaScript / TypeScript ---------------------------------------------------

def _package(project: Path) -> dict:
    try:
        return json.loads((project / "package.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _install_project_deps(project: Path, comp: Component, log) -> None:
    if (project / "node_modules").is_dir():
        return
    npm = shutil.which("npm")
    if not npm:
        raise SetupError("Node.js/npm is not installed, so JavaScript code can't be tested.\n"
                         "Install it with `brew install node`.")
    # npm ci never rewrites the lockfile; fall back to an install that doesn't save changes.
    if (project / "package-lock.json").exists():
        if _run([npm, "ci", "--no-audit", "--no-fund"], project, log).returncode == 0:
            return
    r = _run([npm, "install", "--no-save", "--no-audit", "--no-fund"], project, log)
    if r.returncode != 0:
        raise SetupError(f"Installing the JavaScript dependencies in {comp.dir}/ failed.\n{_tail(r)}")


def _path_aliases(project: Path) -> dict[str, str]:
    """`paths` from tsconfig.json / jsconfig.json, as alias -> directory relative to the project."""
    aliases: dict[str, str] = {}
    for name in ("tsconfig.json", "jsconfig.json"):
        try:
            text = (project / name).read_text()
        except OSError:
            continue
        text = re.sub(r"//.*?$|/\*.*?\*/", "", text, flags=re.S | re.M)   # JSON with comments
        text = re.sub(r",(\s*[}\]])", r"\1", text)                        # trailing commas
        try:
            options = json.loads(text).get("compilerOptions", {})
        except json.JSONDecodeError:
            continue
        base = options.get("baseUrl", ".")
        for key, targets in (options.get("paths") or {}).items():
            if key.endswith("/*") and targets and targets[0].endswith("/*"):
                aliases[key[:-2]] = os.path.normpath(os.path.join(base, targets[0][:-2]))
    if "@" not in aliases and (project / "src").is_dir():
        aliases["@"] = "src"  # the common Vite convention; only matches "@/..." imports
    return aliases


def _harness_files(repo: Path, comp: Component, project: Path) -> dict[str, str]:
    pkg = _package(project)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    comp.dom = any(lib in deps for lib in DOM_LIBS)
    react_major = int(m.group()) if (m := re.search(r"\d+", deps.get("react", ""))) else 0

    dev = {"vitest": "^3.2.4"}
    setup_lines = []
    if comp.dom:
        dev |= {"jsdom": "^26.1.0", "@testing-library/jest-dom": "^6.6.3", "@testing-library/user-event": "^14.6.1"}
        setup_lines.append('import "@testing-library/jest-dom/vitest";')
    if react_major >= 18:
        dev |= {"@testing-library/react": "^16.3.0", "@testing-library/dom": "^10.4.0"}
    elif react_major:
        dev |= {"@testing-library/react": "^12.1.5"}

    harness = repo / comp.tests_dir
    to_project = os.path.relpath(project, harness)
    to_repo = os.path.relpath(repo, harness)
    alias_lines = []
    # Make every import of React resolve to the project's own copy (a second copy breaks hooks).
    for lib in ("react-dom", "react"):
        if (project / "node_modules" / lib).is_dir():
            alias_lines.append(f'    {{ find: /^{lib}(?=$|\\/)/, replacement: here("{to_project}/node_modules/{lib}") }},')
    for alias, target in _path_aliases(project).items():
        alias_lines.append(f'    {{ find: /^{re.escape(alias)}(?=\\/)/, replacement: here("{to_project}/{target}") }},')

    config = f"""// Generated by Agentic QA: runs the tests in this folder against {to_project}.
import {{ defineConfig }} from "vitest/config";
import {{ fileURLToPath }} from "node:url";
import path from "node:path";

const dir = fileURLToPath(new URL(".", import.meta.url));
const here = (p) => path.resolve(dir, p);

export default defineConfig({{
  root: dir,
  cacheDir: here("node_modules/.vite"),
  resolve: {{
    alias: [
{chr(10).join(alias_lines)}
    ],
  }},
  esbuild: {{ jsx: "automatic", loader: "tsx", include: /\\.[cm]?[jt]sx?$/, exclude: [] }},
  server: {{ fs: {{ allow: [here("{to_repo}")] }} }},
  test: {{
    environment: "{'jsdom' if comp.dom else 'node'}",
    globals: true,
    include: ["**/*.{{test,spec}}.{{js,jsx,ts,tsx,mjs,cjs}}"],
    exclude: ["node_modules/**"],
    setupFiles: [{'"./setup.mjs"' if setup_lines else ''}],
    css: false,
    server: {{ deps: {{ inline: [/@testing-library/] }} }},
  }},
}});
"""
    package = {"name": f"agentic-test-{comp.name}".lower(), "private": True, "type": "module",
               "scripts": {"test": "vitest run"}, "devDependencies": dev}
    files = {
        "package.json": json.dumps(package, indent=2) + "\n",
        "vitest.config.mjs": config,
        ".gitignore": "node_modules/\n",
    }
    if setup_lines:
        files["setup.mjs"] = "\n".join(setup_lines) + "\n"
    return files


HARNESS_FILES = ("package.json", "package-lock.json", "vitest.config.mjs", "setup.mjs", ".gitignore")


def setup_js(repo: Path, comp: Component, log) -> None:
    project = repo / comp.dir
    _install_project_deps(project, comp, log)
    harness = repo / comp.tests_dir
    harness.mkdir(parents=True, exist_ok=True)
    for name, content in _harness_files(repo, comp, project).items():
        (harness / name).write_text(content)
    r = _run([shutil.which("npm") or "npm", "install", "--no-audit", "--no-fund", "--legacy-peer-deps"], harness, log)
    if r.returncode != 0:
        raise SetupError(f"Installing the test tools (vitest) for {comp.name} failed.\n{_tail(r)}")
    # Testing libraries `require()` React directly. Link the project's copy in so there is
    # exactly one React instance (two copies break hooks).
    for lib in ("react", "react-dom"):
        source, link = project / "node_modules" / lib, harness / "node_modules" / lib
        if source.is_dir():
            if link.is_symlink() or link.exists():
                shutil.rmtree(link) if link.is_dir() and not link.is_symlink() else link.unlink()
            link.symlink_to(source.resolve(), target_is_directory=True)
    comp.baseline = "not run (JS projects' own test scripts are skipped)"


def _summarize_vitest(output: str) -> str:
    if "No test files found" in output:
        return "no tests"
    match = re.findall(r"^\s*Tests\s+(.+?)\s+\(\d+\)", output, re.M)
    if match:
        return match[-1].replace(" | ", ", ")
    match = re.findall(r"^\s*Test Files\s+(.+?)\s+\(\d+\)", output, re.M)
    return f"test files: {match[-1].replace(' | ', ', ')}" if match else "vitest could not run"


def run_vitest(repo: Path, comp: Component, target: str = "") -> TestRun:
    harness = repo / comp.tests_dir
    vitest = harness / "node_modules" / ".bin" / "vitest"
    cmd = [str(vitest), "run"]
    if target:
        cmd.append(os.path.relpath(repo / target, harness))
    try:
        result = subprocess.run(cmd, cwd=harness, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                                env={**os.environ, "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    except subprocess.TimeoutExpired:
        return TestRun(False, f"timed out after {TEST_TIMEOUT}s", "")
    except FileNotFoundError:
        return TestRun(False, "vitest is not installed", "", has_tests=False)
    output = (result.stdout + "\n" + result.stderr).strip()
    summary = _summarize_vitest(output)
    return TestRun(result.returncode == 0, summary, output, has_tests=summary != "no tests")


# --- entry points -----------------------------------------------------------------

def setup_component(repo: Path, comp: Component, venv_root: Path, log) -> None:
    if comp.lang == "python":
        setup_python(repo, comp, venv_root / comp.name, log)
    else:
        setup_js(repo, comp, log)


def run_component_tests(repo: Path, comp: Component, target: str = "") -> TestRun:
    if comp.lang == "python":
        return run_pytest(repo, comp, target or comp.tests_dir)
    return run_vitest(repo, comp, target)
