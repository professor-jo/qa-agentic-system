"""Run the whole QA process on one repository, independent of how it's shown.

`run_repo` drives a `ui` object, which is either the terminal Dashboard (ui.py) or
a web run (web.py). Both provide the same methods:

    start(key, doing) · done(key, note) · fail(key, note) · skip(key, note)
    activity(who, what) · finish(success, message, details) · changes(data)
    decide(problem, options, default, suggestions) -> key · ask_secret(prompt) -> str
    attributes: title, info, interactive
"""

import json
import os
import shutil
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from qa_team import source
from qa_team.agents import check_llm, model_name, usage_summary
from qa_team.delivery import deliver, remove_leftovers
from qa_team.pipeline import Pipeline, Stop
from qa_team.stack import REPORT_ROOT, TEST_ROOT, SetupError, detect_components, setup_component
from qa_team.summary import collect_changes
from qa_team.tools import Session

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
    details: list[tuple[str, str]] = field(default_factory=list)
    changes: dict | None = None
    repo_path: str = ""
    base_branch: str = ""
    qa_branch: str = ""
    pr_url: str = ""
    test_report: str = ""       # repo-relative paths
    qa_report: str = ""
    usage: str = ""
    model: str = ""
    started: str = ""
    finished: str = ""
    run_dir: str = ""


def new_run_dir(text: str) -> Path:
    name = Path(text.strip().rstrip("/")).name.removesuffix(".git") or "repo"
    run_dir = OUTPUT / name / f"{datetime.now():%Y-%m-%d_%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _open_target(text: str, branch: str | None, ui, log) -> tuple[source.Target, str | None]:
    """Returns (target, branch to restore the stash on, or None if nothing was stashed)."""
    stashed = None
    if source.REMOTE_URL.match(text.strip()):
        url = text.strip()
        dest = source.clone_dir_for(url, WORKSPACE)
        if dest.exists() and (unpushed := source.unpushed_qa_branches(dest)):
            choice = ui.decide(
                f"An earlier clone of {dest.name} has QA work that was never pushed: {', '.join(unpushed)}",
                [("keep", "Move the old clone aside and start fresh"), ("delete", "Delete the old clone")],
                default="keep")
            if choice == "keep":
                moved = source.keep_old_clone(dest)
                ui.activity("Setup", f"kept the old clone at {moved}")
        ui.activity("Setup", f"cloning {url}")
        source.clone(url, dest)
        path, is_clone = dest, True
    else:
        path, is_clone = source.open_local(text), False
        if source.is_dirty(path):
            choice = ui.decide(
                f"{path.name} has uncommitted changes.",
                [("stash", "Stash my changes during the run and restore them afterwards"),
                 ("continue", "Continue anyway (files the agents edit will include my uncommitted edits)"),
                 ("cancel", "Cancel this repository")],
                default="stash",
                suggestions=["Commit or stash your work first so the QA branch only contains the agents' changes."])
            if choice == "cancel":
                raise source.SourceError(f"Cancelled: {path.name} has uncommitted changes.")
            if choice == "stash":
                stashed = source.current_branch(path)   # the stash must go back onto this branch
                source.git(path, "stash", "push", "--include-untracked", "-m", "agentic-qa: before QA run")
    if branch:
        source.checkout(path, branch)
    current = source.current_branch(path)
    label = f"{text.strip() if is_clone else path.name} @ {current}"
    return source.Target(path, current, label, is_clone, source.github_slug(path)), stashed


def _setup_components(target: source.Target, ui, log) -> list:
    components = detect_components(target.path)
    if not components:
        raise SetupError("No Python or JavaScript/TypeScript code was found in this repository.")
    ready = []
    for comp in components:
        while True:
            ui.activity("Setup", f"installing dependencies for {comp.name} ({comp.lang})")
            try:
                setup_component(target.path, comp, WORKSPACE / ".venvs" / target.path.name, log)
                ready.append(comp)
                break
            except SetupError as e:
                first, *rest = str(e).splitlines()
                choice = ui.decide(
                    f"Setting up {comp.name} failed: {first}",
                    [("skip", f"Skip {comp.name} and continue with the other parts"),
                     ("retry", "Retry"), ("stop", "Stop this repository")],
                    default="skip" if len(components) > 1 else "stop",
                    suggestions=rest[-3:] + ["Full output is in the run log."])
                if choice == "retry":
                    continue
                if choice == "stop":
                    raise
                break
    if not ready:
        raise SetupError("No part of the repository could be set up for testing.")
    return ready


def _check_llm(ui) -> None:
    ui.activity("Setup", f"checking the OpenAI model ({model_name()})")
    while (error := check_llm()):
        choice = ui.decide(
            f"Can't use the OpenAI model: {error}",
            [("retry", "Retry"), ("key", "Enter a different API key"), ("stop", "Stop")],
            default="retry",
            suggestions=["Check OPENAI_API_KEY and MODEL in .env.",
                         "If the error says 'quota', add billing at platform.openai.com."])
        if choice == "stop":
            raise SetupError("Stopped: the OpenAI model isn't reachable.")
        if choice == "key":
            if key := ui.ask_secret("New OpenAI API key"):
                os.environ["OPENAI_API_KEY"] = key


def _restore_stash(repo: Path, branch: str, ui, log) -> None:
    """Put the user's stashed edits back on the branch they were made on."""
    if source.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != branch:
        switch = source.git(repo, "checkout", "--quiet", branch)
        if switch.returncode != 0:
            log.write(f"[error] couldn't switch back to {branch}: {switch.stderr}\n")
            ui.activity("Setup", f"your uncommitted changes are safe in the stash; run `git checkout {branch} && "
                                 f"git stash pop` in {repo}")
            return
    pop = source.git(repo, "stash", "pop")
    if pop.returncode != 0:
        log.write(f"[error] restoring your stash failed: {pop.stdout}{pop.stderr}\n")
        ui.activity("Setup", f"your uncommitted changes are safe in the stash; run `git stash pop` on {branch} in {repo}")
    else:
        ui.activity("Setup", f"restored your uncommitted changes on {branch}")


def _save(result: RepoResult) -> RepoResult:
    result.finished = datetime.now().isoformat(timespec="seconds")
    if result.run_dir:
        (Path(result.run_dir) / "result.json").write_text(json.dumps(asdict(result), indent=2))
    return result


def run_repo(text: str, branch: str | None, open_pr: bool, verbose: bool, ui, log, run_dir: Path) -> RepoResult:
    log_path = run_dir / "run.log"
    result = RepoResult(text, False, "", started=datetime.now().isoformat(timespec="seconds"),
                        run_dir=str(run_dir), model=model_name())
    target, stashed, session, test_root_existed = None, None, None, True
    try:
        ui.start("setup", "opening the repository")
        target, stashed = _open_target(text, branch, ui, log)
        ui.title = result.label = target.label
        result.repo_path, result.base_branch = str(target.path), target.branch
        test_root_existed = (target.path / TEST_ROOT).exists()
        components = _setup_components(target, ui, log)
        _check_llm(ui)
        ui.done("setup", ", ".join(f"{c.name} ({c.lang})" for c in components))

        qa_branch = f"qa/agentic-{datetime.now():%Y%m%d-%H%M%S}"
        session = Session(target.path, components, on_activity=ui.activity)
        setup_summary = "\n".join(
            [f"- Repository: {target.label} ({'cloned from GitHub' if target.is_clone else 'local folder'})"]
            + [f"- {c.describe()}; project's existing tests before changes: {c.baseline}" for c in components])
        plan = (f"Changes are committed to the local branch '{qa_branch}' only (nothing is pushed)."
                if not open_pr else
                f"Changes are committed to branch '{qa_branch}'. A pull request into '{target.branch}' is opened "
                "if the GitHub account has write access; otherwise the user chooses fork + PR or a local branch. "
                "Don't claim a PR exists: its link is added to the run summary afterwards.")

        outcome = Pipeline(target.path, target.branch, components, session, ui, log, verbose,
                           setup_summary, plan, lessons_path=OUTPUT / target.path.name / "lessons.json").run()

        ui.start("deliver", "committing")
        summary = "; ".join(f"{n}: {r.summary}" for n, r in outcome.final_results.items())
        body = outcome.qa_report.read_text() if outcome.qa_report else summary
        delivery = deliver(target.path, target.slug, target.branch, qa_branch,
                           sorted(session.source_changes), outcome.passed, summary, body, ui, log, open_pr)
        if delivery.pr_url:
            ui.done("deliver", "PR opened")
        elif delivery.committed:
            ui.skip("deliver", "branch only")
        else:
            ui.fail("deliver", "not committed")

        result.success = outcome.passed and delivery.committed
        result.message = "All tests pass and the work is on its own branch." if result.success else (
            "Tests still fail; see the QA report." if not outcome.passed else "The work couldn't be committed.")
        result.qa_branch = delivery.branch if delivery.committed else ""
        result.pr_url = delivery.pr_url
        result.test_report = str(outcome.test_report.relative_to(target.path)) if outcome.test_report else ""
        result.qa_report = str(outcome.qa_report.relative_to(target.path)) if outcome.qa_report else ""
        result.usage = usage_summary(session.usage)
        result.details = [
            ("Tests", summary),
            ("Fixes", f"{len(session.source_changes)} source file(s) changed"),
            ("Reports", f"{TEST_ROOT}/{outcome.test_report.name if outcome.test_report else '(no test report: tests failing)'}"),
            ("", f"{REPORT_ROOT}/{outcome.qa_report.name}"),
            ("Branch", f"{delivery.branch}" + ("" if delivery.committed else " (not created)")),
            ("PR", delivery.pr_url or delivery.note),
            ("Usage", result.usage),
            ("Where", str(target.path)),
            ("Log", str(log_path)),
        ]
        ui.finish(result.success, result.message, result.details)
        result.changes = collect_changes(target.path, target.branch, result.qa_branch or None,
                                         session.source_changes, outcome.optimization, summary)
        ui.changes(result.changes)
        return _save(result)

    except source.SourceError as e:
        ui.fail("setup")
        result.message, result.details = str(e), [("Log", str(log_path))]
        ui.finish(False, result.message, result.details)
        return _save(result)
    except SetupError as e:
        ui.fail("setup")
        first, *rest = str(e).splitlines()
        result.message, result.details = first, [("", line) for line in rest[-4:]] + [("Log", str(log_path))]
        ui.finish(False, result.message, result.details)
        return _save(result)
    except Stop as e:
        if e.discard and session:
            session.restore_sources()
            if not test_root_existed and (target.path / TEST_ROOT).exists():
                shutil.rmtree(target.path / TEST_ROOT)
            note = "All changes were discarded."
        else:
            note = f"Changes are left uncommitted in {target.path}."
            if session:
                result.changes = collect_changes(target.path, target.branch, None, session.source_changes,
                                                 "", "not all passing")
                ui.changes(result.changes)
        result.message, result.details = f"{e} {note}", [("Log", str(log_path))]
        ui.finish(False, result.message, result.details)
        return _save(result)
    except Exception as e:  # noqa: BLE001 - unexpected bug: log it and keep going with other repos
        log.write(f"[crash] {type(e).__name__}: {e}\n")
        traceback.print_exc(file=log)
        result.message = f"Unexpected error: {type(e).__name__}: {e}"
        result.details = [("Log", str(log_path)), ("", "Please report this with the log file.")]
        ui.finish(False, result.message, result.details)
        return _save(result)
    finally:
        if session:
            result.usage = result.usage or usage_summary(session.usage)
        if target and stashed:
            _restore_stash(target.path, stashed, ui, log)
        if target and target.is_clone is False:
            remove_leftovers(target.path)


def load_history(limit: int = 50) -> list[dict]:
    """Past runs, newest first (from output/<repo>/<timestamp>/result.json)."""
    runs = []
    for path in OUTPUT.glob("*/*/result.json"):
        try:
            runs.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(runs, key=lambda r: r.get("started", ""), reverse=True)[:limit]
