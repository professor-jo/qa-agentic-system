"""Deterministic helpers that keep the agents focused (no LLM calls here).

- triage():        parse failing test output, classify each failure with a knowledge base
                   of known error patterns, and attach a concrete hint
- fingerprint():   identify a round's failures, so a loop that makes no progress is caught
- context_pack():  the failing test and the source lines from its stack trace, pasted into
                   the prompt so agents don't re-explore the repo every round
- repo_brief():    a one-time map of the project shared with every agent
- playbooks():     short how-to-test guides for the detected stack
- Lessons:         per-repo memory of tooling quirks and quarantined tests across runs
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote

from qa_team.stack import IGNORED_DIRS, TEST_ROOT, Component, TestRun

PLAYBOOKS = Path(__file__).parent / "config" / "playbooks"
MAX_CONTEXT_CHARS = 14_000

# (pattern, category, hint). Categories: tooling | environment | test | unknown.
# tooling/environment/test failures go straight to the Test Engineer; unknown ones get a full diagnosis.
KNOWN_ERRORS: list[tuple[str, str, str]] = [
    (r"Failed to parse source for import analysis|contains invalid JS syntax|Transform failed with \d+ error|\[PARSE_ERROR\]",
     "tooling", "The test runner can't load this file type (e.g. .astro/.vue/.svelte, or JSX in a .js file). "
                "Test it a supported way (Astro: render with astro/container) or test plain JS/TS modules instead. "
                "Never change the source code for this."),
    (r"Cannot find (?:module|package) '(?:@testing-library/[^']+|vitest|jsdom|pytest|react-dom/test-utils)'",
     "tooling", "A test-tool import is wrong. Use only the libraries the harness provides (vitest, jsdom, @testing-library/*)."),
    (r"(?:document|window|navigator|localStorage) is not defined",
     "tooling", "The test needs a browser environment it doesn't have. Avoid DOM APIs or test the logic without rendering."),
    (r"Cannot find module '\.{1,2}/|Failed to load url \.{1,2}/|Failed to resolve import \"\.{1,2}/",
     "test", "A relative import in the test is wrong. Paths are relative to the test file inside 'agentic test/'."),
    (r"ModuleNotFoundError|No module named|ImportError: cannot import name",
     "test", "The test imports a module or name that doesn't exist. Import it the way the project's own code does."),
    (r"is not exported|does not provide an export named|has no exported member|is not a function",
     "test", "The test uses something the module doesn't export. Read the module and test its real exports."),
    (r"ECONNREFUSED|ENOTFOUND|getaddrinfo|Connection refused|ConnectError|socket\.gaierror|Network request failed",
     "environment", "The test makes a real network or database call. Mock it (vi.mock / unittest.mock)."),
    (r"Test timed out|exceeded timeout|Timeout of \d+ms|timed out after",
     "environment", "The test hangs, probably waiting on a timer, network or a server. Mock time and I/O."),
    (r"Invalid URL", "test", "A URL is built from a missing base (Astro: create the container with "
                             "astroConfig: { site } and pass a request)."),
    (r"fixture '[^']+' not found", "test", "The test uses a pytest fixture that doesn't exist. Define it in a conftest.py."),
]


@dataclass
class Failure:
    component: str
    test: str
    message: str
    category: str = "unknown"
    hint: str = ""
    locations: list[tuple[str, int]] = field(default_factory=list)   # repo-relative (path, line)


# --- triage ------------------------------------------------------------------------------

def _classify(message: str, block: str) -> tuple[str, str]:
    for pattern, category, hint in KNOWN_ERRORS:
        if re.search(pattern, block):
            return category, hint
    return "unknown", ""


def _locations(block: str, repo: Path, base: Path) -> list[tuple[str, int]]:
    found = []
    # paths may contain the space in "agentic test/"
    for raw, line in re.findall(r"(?:❯|File:|at |^)\s*((?:agentic(?: |%20)test|[^\s:()])+\.(?:py|js|jsx|ts|tsx|mjs|cjs|astro|vue|svelte)):(\d+)", block, re.M):
        path = Path(unquote(raw))
        path = (path if path.is_absolute() else (base / path)).resolve()
        if repo in path.parents and not any(p in IGNORED_DIRS - {TEST_ROOT} for p in path.parts):
            rel = (str(path.relative_to(repo)), int(line))
            if rel not in found:
                found.append(rel)
    return found[:4]


def _vitest_failures(comp: Component, output: str, repo: Path) -> list[Failure]:
    base = repo / comp.tests_dir
    failures = []
    blocks = re.split(r"^\s*FAIL\s+", output, flags=re.M)[1:]
    pending: list[str] = []          # vitest lists several tests above one shared error
    for block in blocks:
        header, _, rest = block.partition("\n")
        if "⎯" in header:
            continue
        chunk = rest.split("⎯⎯⎯", 1)[0]
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            pending.append(header.strip())
            continue
        message = next((ln for ln in lines if re.search(r"Error|expected|assert|→|Cannot|Failed", ln)), lines[0])
        category, hint = _classify(message, header + "\n" + chunk)
        for name in [*pending, header.strip()]:
            failures.append(Failure(comp.name, name[:200], message[:300], category, hint, _locations(chunk, repo, base)))
        pending = []
    for name in pending:
        failures.append(Failure(comp.name, name[:200], "(no error details)", *_classify("", output)))
    return _dedupe(failures)


def _pytest_failures(comp: Component, output: str, repo: Path) -> list[Failure]:
    base = repo / comp.dir
    failures = []
    for test, message in re.findall(r"^(?:FAILED|ERROR) (.+?)(?: - (.*))?$", output, re.M):
        name = test.split("::")[-1]
        section = re.search(rf"_{{3,}} .*{re.escape(name)}.* _{{3,}}\n(.*?)(?=\n_{{3,}} |\n={{3,}} )", output, re.S)
        chunk = section.group(1) if section else output[-3000:]
        category, hint = _classify(message or "", f"{message}\n{chunk}")
        failures.append(Failure(comp.name, test, (message or chunk.strip().splitlines()[-1] if chunk.strip() else "")[:300],
                                category, hint, _locations(chunk, repo, base)))
    if not failures and "error" in output.lower():   # collection errors etc.
        category, hint = _classify(output[-2000:], output[-4000:])
        failures.append(Failure(comp.name, "(collection)", output.strip().splitlines()[-1][:300], category, hint,
                                _locations(output[-4000:], repo, base)))
    return _dedupe(failures)


def _dedupe(failures: list[Failure]) -> list[Failure]:
    seen, unique = set(), []
    for f in failures:
        if (f.test, f.message) not in seen:
            seen.add((f.test, f.message))
            unique.append(f)
    return unique


def triage(repo: Path, components: list[Component], results: dict[str, TestRun]) -> list[Failure]:
    failures: list[Failure] = []
    for comp in components:
        run = results.get(comp.name)
        if not run or run.ok:
            continue
        if not run.has_tests:
            failures.append(Failure(comp.name, "(no tests)", f"No runnable tests in '{comp.tests_dir}/'.", "test",
                                    "Write tests for this component."))
            continue
        parse = _pytest_failures if comp.lang == "python" else _vitest_failures
        found = parse(comp, run.output, repo)
        failures += found or [Failure(comp.name, "(run)", run.summary, *_classify(run.output, run.output[-4000:]))]
    for name, run in results.items():          # regressions in the project's own tests
        if name.endswith("(existing tests)") and not run.ok:
            failures.append(Failure(name, "(existing tests)", run.summary, "unknown",
                                    "The project's own tests passed before the Optimizer's changes; a fix probably broke them."))
    return failures


def fingerprint(failures: list[Failure]) -> frozenset:
    """The same failures with the same errors produce the same fingerprint (numbers ignored)."""
    return frozenset((f.component, f.test, re.sub(r"\d+", "#", f.message)[:120]) for f in failures)


def triage_table(failures: list[Failure]) -> str:
    rows = []
    for f in failures:
        where = ", ".join(f"{p}:{n}" for p, n in f.locations) or "-"
        rows.append(f"- [{f.category.upper()}] {f.component} · {f.test}\n    error: {f.message}\n    at: {where}"
                    + (f"\n    known fix: {f.hint}" if f.hint else ""))
    return "\n".join(rows) or "(none)"


# --- context pack ---------------------------------------------------------------------------

def _excerpt(repo: Path, rel: str, line: int, radius: int = 14) -> str:
    try:
        lines = (repo / rel).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    if len(lines) <= 2 * radius + 10:
        start, end = 1, len(lines)
    else:
        start, end = max(1, line - radius), min(len(lines), line + radius)
    body = "\n".join(f"{i:>5}{'>' if i == line else ' '} {lines[i - 1]}" for i in range(start, end + 1))
    return f"--- {rel} (lines {start}-{end}) ---\n{body}"


def context_pack(repo: Path, failures: list[Failure]) -> str:
    """Relevant code for the failures, so agents don't have to go looking for it."""
    parts, used = [], set()
    for f in failures[:8]:
        for rel, line in f.locations[:2]:
            if (rel, line // 20) in used:
                continue
            used.add((rel, line // 20))
            if text := _excerpt(repo, rel, line):
                parts.append(text)
        if sum(map(len, parts)) > MAX_CONTEXT_CHARS:
            break
    pack = "\n\n".join(parts)
    return pack[:MAX_CONTEXT_CHARS] or "(no source locations found in the failure output; read the failing test files)"


# --- project brief ------------------------------------------------------------------------------

INTERESTING_DEPS = ("react", "next", "vue", "svelte", "astro", "express", "fastify", "vite", "typescript", "jest", "vitest",
                    "fastapi", "flask", "django", "sqlalchemy", "pydantic", "pytest", "requests", "httpx", "pandas")


def _deps(repo: Path, comp: Component) -> list[str]:
    project = repo / comp.dir
    text = ""
    for name in ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"):
        try:
            text += (project / name).read_text(errors="replace").lower() + "\n"
        except OSError:
            pass
    return [d for d in INTERESTING_DEPS if re.search(rf"[\"'\s/^]{re.escape(d)}[\"'\s=<>~\[^,]|^{re.escape(d)}\b", text, re.M)]


def repo_brief(repo: Path, components: list[Component]) -> str:
    lines = ["Components:"]
    for comp in components:
        deps = _deps(repo, comp)
        lines.append(f"- {comp.describe()}" + (f"; key libraries: {', '.join(deps)}" if deps else ""))
        if comp.lang == "js":
            try:
                scripts = json.loads((repo / comp.dir / "package.json").read_text()).get("scripts", {})
                if scripts:
                    lines.append(f"  npm scripts: {', '.join(f'{k}={v}' for k, v in list(scripts.items())[:8])}")
            except (OSError, json.JSONDecodeError):
                pass
        if comp.existing_tests:
            lines.append(f"  existing tests: {comp.existing_tests} (before any change: {comp.baseline})")
    lines.append("Source files (path · lines):")
    count = 0
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not re.search(r"\.(py|js|jsx|ts|tsx|mjs|astro|vue|svelte|html|css|json|toml|yaml|yml|md)$", name):
                continue
            path = Path(dirpath) / name
            try:
                n = sum(1 for _ in path.open(errors="replace"))
            except OSError:
                continue
            lines.append(f"  {path.relative_to(repo)} · {n}")
            count += 1
            if count >= 120:
                lines.append("  … (more files; use list_files)")
                return "\n".join(lines)
    return "\n".join(lines)


def playbooks(repo: Path, components: list[Component]) -> str:
    """How-to-test notes for the stack that's actually in the repo."""
    wanted = []
    for comp in components:
        deps = set(_deps(repo, comp))
        if comp.framework == "astro":
            wanted.append("astro")
        if deps & {"react", "next"}:
            wanted.append("react")
        if deps & {"vue", "svelte"}:
            wanted.append("vue-svelte")
        if deps & {"express", "fastify"}:
            wanted.append("node-server")
        if deps & {"fastapi", "flask", "django"}:
            wanted.append("python-web")
        wanted.append("python" if comp.lang == "python" else "javascript")
    texts = []
    for name in dict.fromkeys(wanted):
        path = PLAYBOOKS / f"{name}.md"
        if path.is_file():
            texts.append(path.read_text().strip())
    return "\n\n".join(texts) or "(no specific playbook)"


# --- lessons across runs ------------------------------------------------------------------------

class Lessons:
    """Per-repo memory (output/<repo>/lessons.json): tooling quirks and quarantined tests."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {"quirks": {}, "quarantined": []}

    def learn(self, failures: list[Failure]) -> None:
        for f in failures:
            if f.category in ("tooling", "environment") and f.hint:
                entry = self.data["quirks"].setdefault(f.hint, {"seen": 0, "example": f.message[:160]})
                entry["seen"] += 1

    def quarantine(self, tests: list[str], reason: str) -> None:
        for t in tests:
            if t not in [q["test"] for q in self.data["quarantined"]]:
                self.data["quarantined"].append({"test": t, "reason": reason[:200]})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def text(self) -> str:
        lines = [f"- {hint} (seen {v['seen']}x, e.g. \"{v['example']}\")" for hint, v in self.data["quirks"].items()]
        lines += [f"- Quarantined before (couldn't be made to pass here): {q['test']}: {q['reason']}" for q in self.data["quarantined"]]
        return "\n".join(lines) or "(no previous runs on this repository)"


def as_dicts(failures: list[Failure]) -> list[dict]:
    return [asdict(f) for f in failures]
