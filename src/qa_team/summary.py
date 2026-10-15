"""The plain "What changed" summary printed in the terminal after a run.

Built from facts, not from what an agent claims: the file list comes from git
(or the tools' change log when nothing was committed), and the one-line reason
for each source change comes from the Optimizer's own change table.
"""

import re
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from qa_team.source import git
from qa_team.stack import HARNESS_FILES, REPORT_ROOT, TEST_ROOT

MAX_REASON = 60


def _numstat(repo: Path, base: str, branch: str | None, paths: list[str]) -> dict[str, tuple[int, int]]:
    """{path: (added, removed)} from git."""
    args = ["diff", "--numstat", f"{base}...{branch}"] if branch else ["diff", "--numstat", "HEAD", "--", *paths]
    stats = {}
    for line in git(repo, *args).stdout.splitlines():
        added, removed, path = line.split("\t", 2)
        stats[path] = (int(added) if added.isdigit() else 0, int(removed) if removed.isdigit() else 0)
    for path in paths:  # new, untracked files don't show up in `git diff`
        if path not in stats and (repo / path).is_file():
            stats[path] = (len((repo / path).read_text(errors="replace").splitlines()), 0)
    return stats


def _reasons(optimization: str) -> dict[str, list[str]]:
    """'| `src/a.py` | what changed | why |' rows -> {'src/a.py': ['what changed']}."""
    reasons: dict[str, list[str]] = {}
    for line in optimization.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]
        looks_like_path = " " not in cells[0] and ("/" in cells[0] or "." in cells[0])
        if len(cells) >= 2 and looks_like_path:
            reasons.setdefault(cells[0], []).append(cells[1])
    return reasons


def _short(text: str) -> str:
    """Keep the first clause of an agent's explanation, without markdown."""
    text = re.sub(r"[`*]", "", text).strip()
    text = re.split(r"(?<=\w)[:;](?=\s)| — | - |\. ", text, maxsplit=1)[0].strip()
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 1].rstrip() + "…"


def _reason_for(path: str, reasons: dict[str, list[str]]) -> str:
    for key, values in reasons.items():
        if key and (path == key or path.endswith("/" + key) or key.endswith(path)):
            return _short(next((v for v in values if v), ""))
    return ""


def _recommendation_count(optimization: str) -> int:
    match = re.search(r"^## Recommendations\s*$(.*?)(?=^## |\Z)", optimization, re.M | re.S)
    return len(re.findall(r"^\s*(?:[-*]|\d+\.)\s+\S", match.group(1), re.M)) if match else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def change_summary(repo: Path, base: str, branch: str | None, source_changes: dict[str, str],
                   optimization: str, test_results: str) -> Panel:
    """`branch` is the QA branch if the work was committed, else None (uncommitted changes)."""
    pending = [*source_changes]
    if not branch:
        pending += [str(p.relative_to(repo)) for root in (TEST_ROOT, REPORT_ROOT)
                    for p in (repo / root).rglob("*") if p.is_file() and "node_modules" not in p.parts]
    stats = _numstat(repo, base, branch, pending)

    source, tests, setup, reports = [], [], [], []
    for path in sorted(stats):
        top = Path(path).parts[0]
        if top == REPORT_ROOT or (top == TEST_ROOT and path.endswith("-test-report.md")):
            reports.append(path)
        elif top == TEST_ROOT:
            (setup if Path(path).name in HARNESS_FILES else tests).append(path)
        else:
            source.append(path)

    reasons = _reasons(optimization)
    lines: list[Text] = []
    width = max((len(p) for p in source), default=0)

    def diffstat(path: str) -> str:
        added, removed = stats[path]
        return f"+{added}" + (f" −{removed}" if removed else "")

    lines.append(Text(f"Code fixed ({_plural(len(source), 'file')})" if source
                      else "Code fixed: none (no confirmed bugs)", style="bold"))
    for path in source:
        new = source_changes.get(path) == "created"
        reason = _reason_for(path, reasons) or ("new file" if new else "")
        lines.append(Text.assemble("  ", ("+ " if new else "✎ ", "green" if new else "yellow"), path.ljust(width),
                                   (f"  {diffstat(path):<9}", "dim"), reason))

    lines.append(Text.assemble((f"Tests added ({_plural(len(tests), 'file')})", "bold"), (f"  {test_results}", "dim")))
    lines += [Text.assemble("  ", ("+ ", "green"), path) for path in tests]

    lines.append(Text("Reports added", style="bold"))
    lines += [Text.assemble("  ", ("+ ", "green"), path) for path in reports]

    if setup:
        folders = ", ".join(sorted({str(Path(p).parent) for p in setup}))
        names = ", ".join(sorted({Path(p).name for p in setup}))
        lines.append(Text.assemble(("Test setup added", "bold"), (f"  in {folders}/: {names}", "dim")))

    footer = []
    if count := _recommendation_count(optimization):
        footer.append(Text(f"Not changed: {_plural(count, 'recommendation')} (UI, frameworks, libraries) "
                           f"are listed in the QA report for you to decide.", style="dim"))
    where = f"on branch {branch}" if branch else "uncommitted, in your working tree"
    for line in lines:  # one line per item; cut with "…" rather than wrapping in narrow terminals
        line.no_wrap, line.overflow = True, "ellipsis"
    return Panel(Group(*lines, *footer), title=f"[bold]What changed[/] · {where}", title_align="left",
                 border_style="dim", padding=(0, 1))
