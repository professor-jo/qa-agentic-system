#!/usr/bin/env python
"""Agentic QA: analyze, fix, test and report on your repositories.

    qa_team                                   # interactive
    qa_team https://github.com/owner/repo     # a GitHub repo (cloned into workspace/)
    qa_team ~/code/project --branch dev       # a local Git repo
    qa_team --mine                            # pick from your GitHub repositories
    qa_team ui                                # open the local web UI in your browser
"""

import argparse
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import date

from dotenv import load_dotenv, set_key

# Before CrewAI is imported: no trace upload and no "view your traces?" prompt.
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")
warnings.filterwarnings("ignore", message="function callbacks cannot be serialized")

from rich.table import Table  # noqa: E402

from qa_team import source  # noqa: E402
from qa_team.agents import DEFAULT_MODEL  # noqa: E402
from qa_team.runner import PROJECT_ROOT, STAGES, new_run_dir, run_repo  # noqa: E402
from qa_team.summary import render_changes  # noqa: E402
from qa_team.ui import Dashboard, ask, console, interactive  # noqa: E402

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
        save_settings({"OPENAI_API_KEY": key, "MODEL": os.getenv("MODEL", DEFAULT_MODEL)})


def save_settings(values: dict[str, str]) -> None:
    """Write settings to the local, git-ignored .env (owner-only permissions)."""
    env_file = PROJECT_ROOT / ".env"
    env_file.touch(mode=0o600, exist_ok=True)
    for key, value in values.items():
        set_key(str(env_file), key, value)


# --- one repository ------------------------------------------------------------------

def process(text: str, branch: str | None, open_pr: bool, verbose: bool) -> RepoResult:
    """Run one repository with the terminal dashboard, then print what changed."""
    run_dir = new_run_dir(text)
    with open(run_dir / "run.log", "w") as log, Dashboard(text, STAGES, log=log, live=not verbose) as dash:
        result = run_repo(text, branch, open_pr, verbose, dash, log, run_dir)
    if result.changes:
        console.print(render_changes(result.changes))
    return RepoResult(result.label, result.success, result.pr_url or result.message)


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
    parser = argparse.ArgumentParser(
        description="Agentic QA: analyze, fix, test and report on Git repositories.",
        epilog="Web UI: run `qa_team ui` (options: --port N, --no-browser).")
    parser.add_argument("repos", nargs="*", help="GitHub URL(s) or local folder(s). Omit to be asked.")
    parser.add_argument("--mine", action="store_true", help="Pick from your own GitHub repositories.")
    parser.add_argument("--branch", help="Branch to QA (default: the repo's current/default branch).")
    parser.add_argument("--no-pr", action="store_true", help="Commit to a local branch only; never push or open a PR.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show the agents' full output instead of the dashboard.")
    return parser.parse_args()


def run() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    if sys.argv[1:2] == ["ui"]:
        from qa_team.web import serve
        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
        serve(port=port, open_browser="--no-browser" not in sys.argv)
        return
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
