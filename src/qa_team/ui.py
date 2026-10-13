"""A single live-updating terminal box: the step list while running, then the result."""

import sys
import time
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

ICONS = {"pending": ("·", "dim"), "done": ("✓", "green"), "failed": ("✗", "red"), "skipped": ("–", "yellow")}


def _elapsed(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 60 else f"{seconds // 60:.0f}m {seconds % 60:02.0f}s"


@dataclass
class Step:
    label: str
    status: str = "pending"
    note: str = ""
    started: float = 0.0
    ended: float = 0.0


@dataclass
class Result:
    success: bool
    message: str
    details: list[tuple[str, str]] = field(default_factory=list)


class StatusBox:
    """Usage:
        with StatusBox(title, ["Step A", "Step B"]) as box:
            box.start(0); ...; box.done("note")
            box.finish(success, message, [("Label", "value")])
    """

    def __init__(self, title: str, labels: list[str], live: bool = True):
        self.title = title
        self.steps = [Step(label) for label in labels]
        self.current: int | None = None
        self.result: Result | None = None
        # Bound to the real terminal, so CrewAI output redirected elsewhere can't break the box.
        self.console = Console(file=sys.__stdout__)
        self.live = Live(self, console=self.console, refresh_per_second=8, transient=False,
                         redirect_stdout=False, redirect_stderr=False) if live else None

    def __enter__(self) -> "StatusBox":
        if self.live:
            self.live.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.current is not None and self.steps[self.current].status == "running":
            self._end("failed", "cancelled" if exc[0] is KeyboardInterrupt else "failed")
        if self.live:
            self.live.stop()
        else:
            self.console.print(self)

    # --- step updates -------------------------------------------------
    def start(self, index: int) -> None:
        self.current = index
        step = self.steps[index]
        step.status, step.started = "running", time.monotonic()

    def _end(self, status: str, note: str) -> None:
        if self.current is None:
            return
        step = self.steps[self.current]
        step.status, step.note, step.ended = status, note, time.monotonic()
        self.current = None

    def done(self, note: str = "") -> None:
        self._end("done", note)

    def fail(self, note: str = "") -> None:
        self._end("failed", note)

    def skip(self, note: str = "") -> None:
        self._end("skipped", note)

    def finish(self, success: bool, message: str, details: list[tuple[str, str]] | None = None) -> None:
        self.result = Result(success, message, details or [])

    # --- rendering ----------------------------------------------------
    def __rich__(self) -> Panel:
        steps = Table.grid(padding=(0, 1))
        steps.add_column(width=1)
        steps.add_column(no_wrap=True)
        steps.add_column(style="dim")
        for step in self.steps:
            if step.status == "running":
                icon = Spinner("dots", style="cyan")
                note = _elapsed(time.monotonic() - step.started)
            else:
                symbol, style = ICONS[step.status]
                icon = Text(symbol, style=style)
                took = f" · {_elapsed(step.ended - step.started)}" if step.ended else ""
                note = f"{step.note}{took}".lstrip(" ·")
            steps.add_row(icon, Text(step.label, style="bold" if step.status == "running" else ""), note)

        parts = [steps]
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
        return Panel(Group(*parts), title=f"[bold]QA Team[/] · {self.title}", title_align="left",
                     border_style=border, padding=(0, 1), expand=False)
