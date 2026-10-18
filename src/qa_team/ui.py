"""Terminal UI: one live dashboard per repository, plus option prompts for errors.

The dashboard lists every stage with its status, shows what the active agent is
doing right now, and keeps a short activity feed. Prompts pause the live view,
ask, and resume it.
"""

import contextlib
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

# Bound to the real terminal so CrewAI output redirected to a log can't break the layout.
console = Console(file=sys.__stdout__, highlight=False)

# Each agent keeps one colour everywhere: its stage label while running and its name in the feed.
AGENT_COLORS = {
    "Code Analyst": "magenta",
    "Optimizer": "yellow",
    "Test Engineer": "green",
    "QA Reporter": "blue",
}


# Each stage animates differently while it runs: (small icon, wider themed animation).
ANIMATIONS = {
    "setup": ("line", "simpleDotsScrolling"),      # loading
    "analyst": ("dots", "point"),                  # scanning the code
    "optimizer": ("squareCorners", "bouncingBar"), # hammering on fixes
    "tester": ("growVertical", "aesthetic"),       # test progress
    "reporter": ("star", "betaWave"),              # writing
    "deliver": ("arc", "bouncingBall"),            # shipping
}
ANIMATION_WIDTH = 8


def agent_name(who: str) -> str:
    """'1 · Code Analyst' -> 'Code Analyst'."""
    return who.split("·", 1)[-1].strip()


def agent_style(who: str) -> str:
    return f"bold {AGENT_COLORS.get(agent_name(who), 'white')}"


ICONS = {"pending": ("·", "dim"), "done": ("✓", "green"), "failed": ("✗", "red"), "skipped": ("–", "yellow")}


def elapsed(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds // 60:.0f}m {seconds % 60:02.0f}s"


@dataclass
class Stage:
    key: str
    label: str
    status: str = "pending"
    note: str = ""
    started: float = 0.0
    total: float = 0.0  # accumulated across loop rounds


@dataclass
class Result:
    success: bool
    message: str
    details: list[tuple[str, str]] = field(default_factory=list)


class Dashboard:
    def __init__(self, title: str, stages: list[tuple[str, str]], log=None, live: bool = True):
        self.title = title
        self.stages = {key: Stage(key, label) for key, label in stages}
        self.doing = ""                       # what the active stage is doing right now
        self.info = ""                        # e.g. "Round 2/3 · last run: 3 failed, 40 passed"
        self.feed: deque[tuple[str, str, str]] = deque(maxlen=6)  # (time, who, what)
        self.result: Result | None = None
        # Spinners are kept between refreshes; a new Spinner always shows its first frame.
        self._spinners: dict[str, tuple[Spinner, Spinner]] = {}
        self.log = log                        # file object; every activity is also written here
        self._live = Live(self, console=console, refresh_per_second=8,
                          redirect_stdout=False, redirect_stderr=False) if live else None

    # --- lifecycle ------------------------------------------------------
    def __enter__(self) -> "Dashboard":
        if self._live:
            self._live.start()
        return self

    def __exit__(self, exc_type, *_):
        for stage in self.stages.values():
            if stage.status == "running":
                self._end(stage, "failed", "cancelled" if exc_type is KeyboardInterrupt else "stopped")
        if self._live:
            self._live.stop()
        else:
            console.print(self)

    @contextlib.contextmanager
    def paused(self):
        """Temporarily stop the live view (for prompts)."""
        if self._live and self._live.is_started:
            self._live.stop()
            try:
                yield
            finally:
                self._live.start()
        else:
            yield

    # --- updates --------------------------------------------------------
    def start(self, key: str, doing: str = "starting") -> None:
        stage = self.stages[key]
        stage.status, stage.started, stage.note = "running", time.monotonic(), ""
        self.activity(stage.label, doing)

    def _end(self, stage: Stage, status: str, note: str) -> None:
        if stage.status == "running":
            stage.total += time.monotonic() - stage.started
        stage.status, stage.note = status, note
        self.doing = ""

    def done(self, key: str, note: str = "") -> None:
        self._end(self.stages[key], "done", note)

    def fail(self, key: str, note: str = "") -> None:
        self._end(self.stages[key], "failed", note)

    def skip(self, key: str, note: str = "") -> None:
        self._end(self.stages[key], "skipped", note)

    def activity(self, who: str, what: str) -> None:
        self.doing = what
        stamp, who = f"{datetime.now():%H:%M:%S}", agent_name(who)
        self.feed.append((stamp, who, what))
        if self.log:
            self.log.write(f"[activity] {stamp}  {who}: {what}\n")
            self.log.flush()

    def finish(self, success: bool, message: str, details: list[tuple[str, str]] | None = None) -> None:
        self.result = Result(success, message, details or [])

    # --- the UI interface shared with the web UI (see web.py: WebRun) ------------
    @property
    def interactive(self) -> bool:
        return interactive()

    def decide(self, problem: str, options: list[tuple[str, str]], default: str,
               suggestions: list[str] | None = None) -> str:
        return choose(problem, options, default, suggestions, dashboard=self, log=self.log)

    def ask_secret(self, prompt: str) -> str:
        with self.paused():
            return ask(prompt, secret=True)

    def changes(self, data: dict) -> None:
        """The web UI shows the 'What changed' data live; the CLI prints it after the box closes."""

    # --- rendering ------------------------------------------------------
    def _animation(self, stage: Stage) -> tuple[Spinner, Spinner]:
        if stage.key not in self._spinners:
            icon, bar = ANIMATIONS.get(stage.key, ("dots", "point"))
            color = agent_style(stage.label).split()[-1] if agent_name(stage.label) in AGENT_COLORS else "cyan"
            self._spinners[stage.key] = (Spinner(icon, style=color), Spinner(bar, style=color))
        return self._spinners[stage.key]

    def __rich__(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=1)
        table.add_column(no_wrap=True)
        table.add_column(width=ANIMATION_WIDTH, no_wrap=True)
        table.add_column(style="dim", overflow="ellipsis", no_wrap=True, max_width=70)
        for stage in self.stages.values():
            animation: Spinner | Text = Text("")
            if stage.status == "running":
                icon, animation = self._animation(stage)
                note = f"{self.doing} · {elapsed(stage.total + time.monotonic() - stage.started)}"
                label = Text(stage.label, style=agent_style(stage.label) if agent_name(stage.label) in AGENT_COLORS else "bold cyan")
            else:
                symbol, style = ICONS[stage.status]
                icon, label = Text(symbol, style=style), Text(stage.label)
                note = " · ".join(part for part in (stage.note, elapsed(stage.total) if stage.total else "") if part)
            table.add_row(icon, label, animation, note)

        parts: list = [table]
        if self.info:
            parts += [Text(self.info, style="yellow")]
        border = "cyan"
        if self.result:
            border = "green" if self.result.success else "red"
            verdict = Text("SUCCESS" if self.result.success else "FAIL", style=f"bold {border}")
            parts += [Rule(style="dim"), Text.assemble(verdict, "  ", self.result.message)]
            if self.result.details:
                details = Table.grid(padding=(0, 2))
                details.add_column(style="dim", no_wrap=True)
                details.add_column(overflow="fold")
                for label, value in self.result.details:
                    details.add_row(label, value)
                parts.append(details)
        elif self.feed:
            feed = Table.grid(padding=(0, 2))
            feed.add_column(style="dim", no_wrap=True)                       # time
            feed.add_column(no_wrap=True)                                    # agent
            feed.add_column(overflow="ellipsis", no_wrap=True)              # action
            for stamp, who, what in self.feed:
                feed.add_row(stamp, Text(who, style=agent_style(who)), what)
            parts += [Rule("activity", style="dim", align="left"), feed]
        return Panel(Group(*parts), title=f"[bold]Agentic QA[/] · {self.title}", title_align="left",
                     border_style=border, padding=(0, 1))


# --- option prompts -------------------------------------------------------

def interactive() -> bool:
    return sys.stdin.isatty()


def choose(problem: str, options: list[tuple[str, str]], default: str,
           suggestions: list[str] | None = None, dashboard: Dashboard | None = None, log=None) -> str:
    """Show a problem with suggested fixes and let the user pick an option.

    `options` is [(key, label)]. Returns the chosen key. Without a terminal
    (piped input, CI) the default is chosen and logged.
    """
    if log:
        log.write(f"[error] {problem}\n")
        for s in suggestions or []:
            log.write(f"[suggestion] {s}\n")
    if not interactive():
        if log:
            log.write(f"[choice] non-interactive, using default: {default}\n")
        return default

    context = dashboard.paused() if dashboard else contextlib.nullcontext()
    with context:
        body = [Text(problem, style="bold")]
        if suggestions:
            body += [Text(""), Text("Suggestions:", style="dim")] + [Text(f"  • {s}") for s in suggestions]
        body += [Text("")]
        keys = {}
        for i, (key, label) in enumerate(options, start=1):
            keys[str(i)] = key
            marker = " (default)" if key == default else ""
            body.append(Text.assemble((f"  {i}) ", "bold"), label, (marker, "dim")))
        console.print(Panel(Group(*body), title="[bold yellow]Needs your decision", border_style="yellow"))
        default_number = next(n for n, k in keys.items() if k == default)
        while True:
            try:
                answer = console.input(f"Choose [1-{len(options)}] (Enter = {default_number}): ").strip()
            except EOFError:
                answer = ""
            answer = answer or default_number
            if answer in keys:
                if log:
                    log.write(f"[choice] {keys[answer]}\n")
                return keys[answer]
            console.print(f"[red]Please enter a number from 1 to {len(options)}.[/]")


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = console.input(f"[bold]{prompt}{suffix}:[/] ", password=secret).strip()
    except EOFError:
        answer = ""
    return answer or default
