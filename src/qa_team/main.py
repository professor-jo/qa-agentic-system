#!/usr/bin/env python
"""Agentic QA: analyze, fix, test and report on your repositories.

    qa_team                                   # interactive
    qa_team https://github.com/owner/repo     # a GitHub repo (cloned into workspace/)
    qa_team ~/code/project --branch dev       # a local Git repo
    qa_team --mine                            # pick from your GitHub repositories
"""

import argparse
import os
import shutil
import sys
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv, set_key

# Before CrewAI is imported: no trace upload and no "view your traces?" prompt.
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")
warnings.filterwarnings("ignore", message="function callbacks cannot be serialized")

from rich.table import Table  # noqa: E402

from qa_team import source  # noqa: E402
from qa_team.agents import check_llm, usage_summary  # noqa: E402
from qa_team.delivery import deliver, remove_leftovers  # noqa: E402
from qa_team.pipeline import Pipeline, Stop  # noqa: E402
from qa_team.stack import TEST_ROOT, SetupError, detect_components, setup_component  # noqa: E402
from qa_team.summary import change_summary  # noqa: E402
from qa_team.tools import Session  # noqa: E402
from qa_team.ui import Dashboard, ask, choose, console, interactive  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = PROJECT_ROOT / "workspace"
OUTPUT = PROJECT_ROOT / "output"
STAGES = [
    ("setup", "Setup"),
    ("analyst", "1 · Code Analyst"),
    ("optimizer", "2 · Optimizer"),
    ("tester", "3 · Test Engineer"),
    ("reporter", "4 · QA Reporter"),
    ("deliver", "Branch & PR"),
]


@dataclass
class RepoResult:
    label: str
    success: bool
    message: str


# --- LLM key -----------------------------------------------------------------------

def ensure_api_key() -> None:
    """The agents need an OpenAI key. Ask for it if it isn't configured."""
    if os.getenv("OPENAI_API_KEY"):
        return
    if not interactive():
        sys.exit("OPENAI_API_KEY is not set. Add it to .env (see .env.example).")
    console.print("[bold]The agents need an OpenAI API key[/] (get one at platform.openai.com/api-keys).")
    key = ask("OpenAI API key (input hidden)", secret=True)
    if not key:
        sys.exit("No API key given.")
    os.environ["OPENAI_API_KEY"] = key
    if ask("Save it to .env for next time? (Y/n)", "y").lower().startswith("y"):
        env_file = PROJECT_ROOT / ".env"
        env_file.touch(mode=0o600, exist_ok=True)
        set_key(str(env_file), "OPENAI_API_KEY", key)
        set_key(str(env_file), "MODEL", os.getenv("MODEL", "gpt-6.1-sol"))


# --- one repository ------------------------------------------------------------------

def _open_target(text: str, branch: str | None, dash: Dashboard, log) -> tuple[source.Target, bool]:
    """Returns (target, stashed)."""
    stashed = False
    if source.REMOTE_URL.match(text.strip()):
        url = text.strip()
        dest = source.clone_dir_for(url, WORKSPACE)
        if dest.exists() and (unpushed := source.unpushed_qa_branches(dest)):
            choice = choose(
                f"An earlier clone of {dest.name} has QA work that was never pushed: {', '.join(unpushed)}",
                [("keep", "Move the old clone aside and start fresh"), ("delete", "Delete the old clone")],
                default="keep", dashboard=dash, log=log)
            if choice == "keep":
                moved = source.keep_old_clone(dest)
                dash.activity("Setup", f"kept the old clone at {moved}")
        dash.activity("Setup", f"cloning {url}")
        source.clone(url, dest)
        path, is_clone = dest, True
    else:
        path, is_clone = source.open_local(text), False
        if source.is_dirty(path):
            choice = choose(
                f"{path.name} has uncommitted changes.",
                [("stash", "Stash my changes during the run and restore them afterwards"),
                 ("continue", "Continue anyway (files the agents edit will include my uncommitted edits)"),
                 ("cancel", "Cancel this repository")],
                default="stash",
                suggestions=["Commit or stash your work first so the QA branch only contains the agents' changes."],
                dashboard=dash, log=log)
            if choice == "cancel":
                raise source.SourceError(f"Cancelled: {path.name} has uncommitted changes.")
            if choice == "stash":
                source.git(path, "stash", "push", "--include-untracked", "-m", "agentic-qa: before QA run")
                stashed = True
    if branch:
        source.checkout(path, branch)
    current = source.current_branch(path)
    label = f"{text.strip() if is_clone else path.name} @ {current}"
    return source.Target(path, current, label, is_clone, source.github_slug(path)), stashed


def _setup_components(target: source.Target, dash: Dashboard, log) -> list:
    components = detect_components(target.path)
    if not components:
        raise SetupError("No Python or JavaScript/TypeScript code was found in this repository.")
    ready = []
    for comp in components:
        while True:
            dash.activity("Setup", f"installing dependencies for {comp.name} ({comp.lang})")
            try:
                setup_component(target.path, comp, WORKSPACE / ".venvs" / target.path.name, log)
                ready.append(comp)
                break
            except SetupError as e:
                first, *rest = str(e).splitlines()
                choice = choose(
                    f"Setting up {comp.name} failed: {first}",
                    [("skip", f"Skip {comp.name} and continue with the other parts"),
                     ("retry", "Retry"), ("stop", "Stop this repository")],
                    default="skip" if len(components) > 1 else "stop",
                    suggestions=rest[-3:] + ["Full output is in the run log."],
                    dashboard=dash, log=log)
                if choice == "retry":
                    continue
                if choice == "stop":
                    raise
                break
    if not ready:
        raise SetupError("No part of the repository could be set up for testing.")
    return ready


def _check_llm(dash: Dashboard, log) -> None:
    while (error := check_llm()):
        choice = choose(
            f"Can't use the OpenAI model: {error}",
            [("retry", "Retry"), ("key", "Enter a different API key"), ("stop", "Stop")],
            default="retry",
            suggestions=["Check OPENAI_API_KEY and MODEL in .env.",
                         "If the error says 'quota', add billing at platform.openai.com."],
            dashboard=dash, log=log)
        if choice == "stop":
            raise SetupError("Stopped: the OpenAI model isn't reachable.")
        if choice == "key":
            with dash.paused():
                os.environ["OPENAI_API_KEY"] = ask("New OpenAI API key (input hidden)", secret=True)


def process(text: str, branch: str | None, open_pr: bool, verbose: bool) -> RepoResult:
    """Run one repository; the 'What changed' summary prints after the dashboard closes."""
    after: list = []
    result = _process(text, branch, open_pr, verbose, after)
    for renderable in after:
        console.print(renderable)
    return result


def _process(text: str, branch: str | None, open_pr: bool, verbose: bool, after: list) -> RepoResult:
    name = Path(text.rstrip("/")).name.removesuffix(".git") or "repo"
    run_dir = OUTPUT / name / f"{datetime.now():%Y-%m-%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"

    with open(log_path, "w") as log, Dashboard(text, STAGES, log=log, live=not verbose) as dash:
        target, stashed, session, test_root_existed = None, False, None, True
        try:
            dash.start("setup", "opening the repository")
            target, stashed = _open_target(text, branch, dash, log)
            dash.title = target.label
            test_root_existed = (target.path / TEST_ROOT).exists()
            components = _setup_components(target, dash, log)
            _check_llm(dash, log)
            dash.done("setup", ", ".join(f"{c.name} ({c.lang})" for c in components))

            qa_branch = f"qa/agentic-{datetime.now():%Y%m%d-%H%M%S}"
            session = Session(target.path, components, on_activity=dash.activity)
            setup_summary = "\n".join(
                [f"- Repository: {target.label} ({'cloned from GitHub' if target.is_clone else 'local folder'})"]
                + [f"- {c.describe()}; project's existing tests before changes: {c.baseline}" for c in components])
            plan = (f"Changes are committed to the local branch '{qa_branch}' only (run with --no-pr; nothing is pushed)."
                    if not open_pr else
                    f"Changes are committed to branch '{qa_branch}'. A pull request into '{target.branch}' is opened "
                    "if the GitHub account has write access; otherwise the user chooses fork + PR or a local branch. "
                    "Don't claim a PR exists: its link is added to the run summary afterwards.")

            pipeline = Pipeline(target.path, target.branch, components, session, dash, log, verbose,
                                setup_summary, plan)
            outcome = pipeline.run()

            dash.start("deliver", "committing")
            summary = "; ".join(f"{n}: {r.summary}" for n, r in outcome.final_results.items())
            body = outcome.qa_report.read_text() if outcome.qa_report else summary
            delivery = deliver(target.path, target.slug, target.branch, qa_branch,
                               sorted(session.source_changes), outcome.passed, summary, body, dash, log, open_pr)
            if delivery.pr_url:
                dash.done("deliver", "PR opened")
            elif delivery.committed:
                dash.skip("deliver", "branch only")
            else:
                dash.fail("deliver", "not committed")

            details = [
                ("Tests", summary),
                ("Fixes", f"{len(session.source_changes)} source file(s) changed"),
                ("Reports", f"{TEST_ROOT}/{outcome.test_report.name if outcome.test_report else '(no test report: tests failing)'}"),
                ("", f"Agentic QA Report/{outcome.qa_report.name}"),
                ("Branch", f"{delivery.branch}" + ("" if delivery.committed else " (not created)")),
                ("PR", delivery.pr_url or delivery.note),
                ("Usage", usage_summary(session.usage)),
                ("Where", str(target.path)),
                ("Log", str(log_path)),
            ]
            ok = outcome.passed and delivery.committed
            message = "All tests pass and the work is on its own branch." if ok else (
                "Tests still fail; see the QA report." if not outcome.passed else "The work couldn't be committed.")
            dash.finish(ok, message, details)
            after.append(change_summary(
                target.path, target.branch, delivery.branch if delivery.committed else None,
                session.source_changes, outcome.optimization,
                "; ".join(f"{n}: {r.summary}" for n, r in outcome.final_results.items())))
            return RepoResult(target.label if target else text, ok, delivery.pr_url or message)

        except source.SourceError as e:
            dash.fail("setup")
            dash.finish(False, str(e), [("Log", str(log_path))])
            return RepoResult(text, False, str(e).splitlines()[0])
        except SetupError as e:
            dash.fail("setup")
            first, *rest = str(e).splitlines()
            dash.finish(False, first, [("", line) for line in rest[-4:]] + [("Log", str(log_path))])
            return RepoResult(text, False, first)
        except Stop as e:
            if e.discard and session:
                session.restore_sources()
                if not test_root_existed and (target.path / TEST_ROOT).exists():
                    shutil.rmtree(target.path / TEST_ROOT)
                note = "All changes were discarded."
            else:
                note = f"Changes are left uncommitted in {target.path}."
                after.append(change_summary(target.path, target.branch, None, session.source_changes if session else {},
                                            "", "not all passing"))
            dash.finish(False, f"{e} {note}", [("Log", str(log_path))])
            return RepoResult(text, False, str(e))
        except Exception as e:  # noqa: BLE001 - unexpected bug: log it and keep going with other repos
            log.write(f"[crash] {type(e).__name__}: {e}\n")
            import traceback
            traceback.print_exc(file=log)
            dash.finish(False, f"Unexpected error: {type(e).__name__}: {e}",
                        [("Log", str(log_path)), ("", "Please report this with the log file.")])
            return RepoResult(text, False, f"{type(e).__name__}: {e}")
        finally:
            if target and stashed:
                pop = source.git(target.path, "stash", "pop")
                if pop.returncode != 0:
                    log.write(f"[error] restoring your stash failed: {pop.stderr}\n")
                    console.print(f"[yellow]Your stashed changes couldn't be restored automatically; "
                                  f"run `git stash pop` in {target.path}.[/]")
            if target and target.is_clone is False:
                remove_leftovers(target.path)


# --- input ----------------------------------------------------------------------

def pick_from_github() -> list[str]:
    try:
        repos = source.list_my_repos()
    except source.SourceError as e:
        console.print(f"[red]{e}[/]")
        return []
    if not repos:
        console.print("[yellow]No repositories found on your GitHub account.[/]")
        return []
    table = Table(title="Your GitHub repositories", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Repository")
    table.add_column("Language")
    table.add_column("Updated")
    for i, r in enumerate(repos, start=1):
        table.add_row(str(i), r["nameWithOwner"] + (" (fork)" if r["isFork"] else ""),
                      (r.get("primaryLanguage") or {}).get("name", "-"), r["updatedAt"][:10])
    console.print(table)
    while True:
        text = ask("Which ones? (e.g. 1,3-4 or all; Enter to cancel)")
        if not text:
            return []
        try:
            return [repos[i]["url"] for i in source.parse_selection(text, len(repos))]
        except ValueError as e:
            console.print(f"[red]{e}[/]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic QA: analyze, fix, test and report on Git repositories.")
    parser.add_argument("repos", nargs="*", help="GitHub URL(s) or local folder(s). Omit to be asked.")
    parser.add_argument("--mine", action="store_true", help="Pick from your own GitHub repositories.")
    parser.add_argument("--branch", help="Branch to QA (default: the repo's current/default branch).")
    parser.add_argument("--no-pr", action="store_true", help="Commit to a local branch only; never push or open a PR.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show the agents' full output instead of the dashboard.")
    return parser.parse_args()


def run() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    try:
        repos = list(args.repos)
        if args.mine:
            repos += pick_from_github()
        if not repos:
            if not interactive():
                sys.exit("Give a repository URL or path, or use --mine.")
            console.print("[bold]Agentic QA[/]: analyze, fix, test and report on a repository.\n")
            console.print("Which repository should the team work on? Enter one of:")
            console.print("  • a [bold]local folder path[/]   e.g. [cyan]~/projects/my-app[/]")
            console.print("    [dim]tip: drag the folder from Finder into this window to paste its path[/]")
            console.print("  • a [bold]GitHub URL[/]          e.g. [cyan]https://github.com/owner/repo[/]")
            console.print("  • [bold]nothing[/] (just press Enter) to pick from your GitHub repositories")
            console.print("[dim]  Local folders must be Git repositories (run `git init` in them first if not).[/]\n")
            text = ask("Folder path or GitHub URL")
            repos = [text] if text else pick_from_github()
            if not repos:
                sys.exit(0)
            if len(repos) == 1 and not args.branch:
                args.branch = ask("Branch (Enter for the current/default branch)") or None
        ensure_api_key()

        results = []
        for i, text in enumerate(repos, start=1):
            if len(repos) > 1:
                console.rule(f"[bold]{i}/{len(repos)}[/] {text}")
            results.append(process(text, args.branch if len(repos) == 1 else None, not args.no_pr, args.verbose))

        if len(results) > 1:
            table = Table(title=f"Agentic QA · {date.today().isoformat()}")
            table.add_column("Repository")
            table.add_column("Result")
            table.add_column("PR / note", overflow="fold")
            for r in results:
                table.add_row(r.label, "[green]SUCCESS[/]" if r.success else "[red]FAIL[/]", r.message)
            console.print(table)
        sys.exit(0 if all(r.success for r in results) else 1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        sys.exit(130)


if __name__ == "__main__":
    run()
