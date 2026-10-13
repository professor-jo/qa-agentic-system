#!/usr/bin/env python
"""Run the QA team against a Git repository.

    qa_team                                        # interactive: asks which repo to test
    qa_team https://github.com/owner/repo          # clone and test the default branch
    qa_team https://github.com/owner/repo --branch dev
    qa_team /path/to/local/repo --no-pr

Steps: analyze the code -> find bugs & optimizations -> write, run & report a test
suite -> if you have write access, push the tests and open a pull request.
"""

import argparse
import contextlib
import getpass
import os
import re
import sys
import warnings

from dotenv import load_dotenv

# Must be set before CrewAI is imported: no trace upload and no interactive
# "view your traces?" prompt at the end of a run.
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from qa_team.crew import PROJECT_ROOT, QATeam, build_llm  # noqa: E402
from qa_team.publish import publish  # noqa: E402
from qa_team.repo_env import EnvError, RepoEnv, build_env  # noqa: E402
from qa_team.repo_source import RepoError, RepoTarget, resolve  # noqa: E402
from qa_team.tools.repo_tools import RunTestsTool  # noqa: E402
from qa_team.ui import StatusBox  # noqa: E402

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")
# CrewAI warns that task callbacks block checkpointing, which we don't use.
warnings.filterwarnings("ignore", message="function callbacks cannot be serialized")

WORKSPACE = PROJECT_ROOT / "workspace"
YELLOW, BOLD, RESET = "\033[33m", "\033[1m", "\033[0m"
STEPS = {
    "analyze_code_task": "Analyze code",
    "find_issues_task": "Find bugs & optimizations",
    "write_and_run_tests_task": "Write & run tests",
}
def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{BOLD}{prompt}{suffix}: {RESET}").strip()
    except EOFError:
        answer = ""
    return answer or default


def ensure_api_key(interactive: bool) -> None:
    if os.getenv("OPENAI_API_KEY"):
        return
    if not interactive:
        sys.exit("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")
    key = getpass.getpass("OPENAI_API_KEY is not set. Paste it here (input hidden): ").strip()
    if not key:
        sys.exit("No API key given.")
    os.environ["OPENAI_API_KEY"] = key


def check_llm() -> str | None:
    """Make one tiny call so a bad key, unknown model or unsupported parameter
    fails fast with a clear message instead of stalling mid-crew."""
    llm = build_llm()
    try:
        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull):
            llm.call("Reply with the single word OK.")
    except Exception as e:
        return f"{llm.model}: {type(e).__name__}: {e}"
    return None


def summarize_pytest(output: str) -> str:
    """Pull pytest's final summary ('3 passed, 1 failed in 0.4s') out of its output."""
    matches = re.findall(r"^=*\s*(\d+ (?:passed|failed|error).*?) in [\d.]+s", output, re.M)
    if matches:
        return matches[-1]
    if "no tests ran" in output or "exit code: 4" in output and "not found" in output:
        return "none yet"
    return "could not parse pytest output"


def run_suite(target: RepoTarget, env: RepoEnv) -> tuple[str, str]:
    """Run the repo's tests directory outside the agents. Returns (raw output, summary)."""
    tool = RunTestsTool(
        repo_root=str(target.path), tests_dir=env.tests_dir, project_dir=env.project_dir, python=env.python
    )
    output = tool._run()
    return output, summarize_pytest(output)


def test_one(source: str, branch: str | None, tests_dir: str | None, verbose: bool,
             setup_only: bool = False, create_pr: bool = True) -> bool:
    labels = ["Get repository", "Install dependencies"]
    if not setup_only:
        labels += [*STEPS.values(), "Pull request"]
    with StatusBox(source, labels, live=not verbose) as box:
        return _run_steps(box, source, branch, tests_dir, verbose, setup_only, create_pr)


def _run_steps(box: StatusBox, source: str, branch: str | None, tests_dir: str | None,
               verbose: bool, setup_only: bool, create_pr: bool) -> bool:
    box.start(0)
    try:
        target = resolve(source, WORKSPACE, branch=branch)
    except RepoError as e:
        box.fail()
        box.finish(False, str(e))
        return False
    box.title = target.label
    box.done(f"branch {target.branch}")

    box.start(1)
    try:
        env = build_env(target.path, WORKSPACE / ".venvs" / target.path.name, tests_dir)
    except EnvError as e:
        box.fail()
        first, *rest = str(e).splitlines()
        box.finish(False, first, [("", line) for line in rest[-8:]])
        return False
    baseline_output, baseline = run_suite(target, env)
    box.done(f"existing tests: {baseline}")

    if setup_only:
        ok = "exit code: 0" in baseline_output or baseline == "none yet"
        box.finish(ok, "Ready to test." if ok else "Existing tests fail.",
                   [("Tests in", f"{env.tests_dir}/ (run from {env.project_dir}/)")])
        return ok

    if error := check_llm():
        box.finish(False, "Could not reach the OpenAI model. Check OPENAI_API_KEY and MODEL.", [("Error", error[:300])])
        return False

    # Steps 1-3: the crew. Its detailed output goes to run.log unless --verbose.
    first_crew_step = 2
    step_index = {name: first_crew_step + i for i, name in enumerate(STEPS)}

    def on_task_done(name: str) -> None:
        box.done()
        if step_index[name] + 1 < first_crew_step + len(STEPS):
            box.start(step_index[name] + 1)

    team = QATeam(repo_path=str(target.path), env=env, verbose=verbose, on_task_done=on_task_done)
    team.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = team.output_dir / "run.log"
    inputs = {
        "repo_path": str(target.path),
        "repo_name": target.path.name,
        "branch": target.branch,
        "tests_dir": env.tests_dir,
        "project_dir": env.project_dir,
        "baseline": baseline,
    }
    crew = team.crew()
    box.start(first_crew_step)
    try:
        with open(log_path, "w") as log, contextlib.ExitStack() as stack:
            if not verbose:
                stack.enter_context(contextlib.redirect_stdout(log))
                stack.enter_context(contextlib.redirect_stderr(log))
            crew.kickoff(inputs=inputs)
    except Exception as e:  # LLM/API errors, rate limits, tool crashes
        box.fail()
        team.save_reports(crew)  # keep whatever finished before the crash
        box.finish(False, "The QA team crashed before finishing.",
                   [("Error", f"{type(e).__name__}: {e}"[:300]), ("Log", str(log_path))])
        return False
    reports = team.save_reports(crew)

    # Judge the result by actually running the suite, not by trusting the LLM's summary.
    test_output, summary = run_suite(target, env)
    tests_passed = "exit code: 0" in test_output
    new_files = sorted(team.write_tool.written)

    # Step 4: push and open a PR if we have write access.
    box.start(first_crew_step + len(STEPS))
    if not create_pr:
        pr_line = "skipped (--no-pr)"
        box.skip("--no-pr")
    elif target.from_pr:
        pr_line = "skipped: the input was a pull request URL"
        box.skip("input was a PR")
    else:
        result = publish(target.path, target.branch, new_files, summary, tests_passed, reports)
        pr_line = result.message
        if result.created:
            box.done("created")
        else:
            box.skip("no write access" if "write access" in result.message else "skipped")

    details = [
        ("Tests", f"{summary}  (before: {baseline}, {len(new_files)} new file(s))"),
        ("PR", pr_line),
        ("Reports", f"{team.output_dir}/"),
    ]
    if tests_passed:
        box.finish(True, "All tests pass.", details)
    elif "exit code: 5" in test_output:
        box.finish(False, "No runnable tests were produced.", details)
    else:
        box.finish(False, "Some tests fail. They may have found bugs; see TEST_REPORT.md.", details)
    return tests_passed


def interactive(args: argparse.Namespace) -> int:
    print(f"{BOLD}QA Team{RESET}. Enter a GitHub repo URL, PR URL or local path (empty to quit).")
    last_ok = True
    while True:
        source = ask("\nRepository to test")
        if not source:
            return 0 if last_ok else 1
        branch = None
        if "/pull/" not in source:
            branch = ask("Branch to test (Enter for the default branch)") or None
        print()
        last_ok = test_one(source, branch, args.tests_dir, args.verbose, args.setup_only, not args.no_pr)
        if ask("\nTest another repository? (y/N)", "n").lower() not in ("y", "yes"):
            return 0 if last_ok else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CrewAI QA team on a Git repository.")
    parser.add_argument("repo", nargs="?", help="Git URL, GitHub PR URL or local path. Omit to be asked.")
    parser.add_argument("--branch", help="Branch to check out and test (default: the repo's current/default branch).")
    parser.add_argument("--tests-dir", help="Where tests live / new tests go, relative to the repo (default: auto-detect).")
    parser.add_argument("--no-pr", action="store_true", help="Never push or open a pull request.")
    parser.add_argument("--setup-only", action="store_true", help="Clone and install dependencies, run existing tests, skip the agents (no API key needed).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show the agents' full step-by-step output.")
    return parser.parse_args()


def run() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    if not args.setup_only:
        ensure_api_key(interactive=args.repo is None)
    try:
        if args.repo is None:
            sys.exit(interactive(args))
        ok = test_one(args.repo, args.branch, args.tests_dir, args.verbose, args.setup_only, not args.no_pr)
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Cancelled.{RESET}")
        sys.exit(130)


if __name__ == "__main__":
    run()
