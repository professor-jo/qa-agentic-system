"""The QA process for one repository, after setup:

    Analyst -> Optimizer -> Test Engineer -> verify
         ^                                     | tests fail
         +---- diagnose -> fix code -> repair tests
    all pass -> test report -> QA Reporter
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from qa_team.agents import run_task
from qa_team.stack import REPORT_ROOT, TEST_ROOT, Component, TestRun, run_component_tests, run_pytest
from qa_team.tools import Session
from qa_team.ui import Dashboard, choose, interactive

ROUNDS_PER_BATCH = 3
MAX_FAILURE_CHARS = 12_000


class Stop(Exception):
    """The user chose to stop. `discard` says whether to throw away the changes."""

    def __init__(self, message: str, discard: bool = False):
        super().__init__(message)
        self.discard = discard


@dataclass
class Outcome:
    passed: bool
    final_results: dict[str, TestRun]
    rounds: list[str] = field(default_factory=list)
    test_report: Path | None = None
    qa_report: Path | None = None
    optimization: str = ""   # the Optimizer's change table + recommendations (for the summary)


def unique_path(path: Path) -> Path:
    """'x.md' -> 'x (2).md' if x.md already exists (an earlier run the same day)."""
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


class Pipeline:
    def __init__(self, repo: Path, branch: str, components: list[Component], session: Session,
                 dashboard: Dashboard, log, verbose: bool, setup_summary: str, delivery_plan: str):
        self.repo, self.branch, self.components = repo, branch, components
        self.session, self.dash, self.log, self.verbose = session, dashboard, log, verbose
        self.setup_summary, self.delivery_plan = setup_summary, delivery_plan
        self.today = date.today().isoformat()

    # --- helpers --------------------------------------------------------------
    def _base_inputs(self) -> dict:
        return {
            "repo_path": str(self.repo), "repo_name": self.repo.name, "branch": self.branch,
            "components": "\n".join(f"- {c.describe()}" for c in self.components),
            "test_root": TEST_ROOT, "report_root": REPORT_ROOT, "date": self.today,
        }

    def _task(self, stage: str, task_key: str, doing: str, **inputs) -> str:
        """Run one agent task. On an error (API outage, rate limit, ...) ask what to do."""
        while True:
            self.dash.start(stage, doing)
            try:
                result = run_task(task_key, self.session, {**self._base_inputs(), **inputs}, self.log, self.verbose)
                self.dash.done(stage)
                return result
            except Exception as e:  # noqa: BLE001 - anything the LLM/tools raise
                self.dash.fail(stage, "error")
                choice = choose(
                    f"The {self.session.agent} hit an error: {type(e).__name__}: {str(e)[:300]}",
                    [("retry", "Retry this step"),
                     ("stop", "Stop here and keep the changes made so far (no report, no PR)"),
                     ("discard", "Stop and discard all changes")],
                    default="retry",
                    suggestions=_error_suggestions(e),
                    dashboard=self.dash, log=self.log,
                )
                if choice != "retry":
                    raise Stop(f"Stopped after an error in the {self.session.agent}.", discard=choice == "discard")

    def _verify(self) -> tuple[bool, dict[str, TestRun], str]:
        """Run every component's tests outside the agents (plus the project's own
        Python tests if they passed before, to catch regressions)."""
        self.dash.activity("Verifier", "running the full test suite")
        results, failures = {}, []
        for comp in self.components:
            run = run_component_tests(self.repo, comp)
            results[comp.name] = run
            if not run.has_tests:
                failures.append(f"## {comp.name}: NO TESTS\nNo runnable tests were found in '{comp.tests_dir}/'. Write them.")
            elif not run.ok:
                failures.append(f"## {comp.name}: FAIL ({run.summary})\n{run.output[-MAX_FAILURE_CHARS // 2:]}")
            if comp.lang == "python" and comp.existing_tests and comp.baseline_ok:
                regression = run_pytest(self.repo, comp, comp.existing_tests, importlib_mode=False)
                if not regression.ok:
                    results[f"{comp.name} (existing tests)"] = regression
                    failures.append(
                        f"## {comp.name}: the project's OWN tests in '{comp.existing_tests}' now fail "
                        f"(they passed before the Optimizer's changes: {comp.baseline})\n{regression.output[-MAX_FAILURE_CHARS // 2:]}"
                    )
        ok = not failures
        summary = "; ".join(f"{name}: {run.summary}" for name, run in results.items())
        self.dash.info = f"Last test run: {summary}"
        self.log.write(f"\n[verify] {'PASS' if ok else 'FAIL'}: {summary}\n")
        if failures:
            self.log.write("\n\n".join(failures)[-MAX_FAILURE_CHARS:] + "\n")
        self.log.flush()
        return ok, results, "\n\n".join(failures)[-MAX_FAILURE_CHARS:]

    # --- the process ------------------------------------------------------------
    def run(self) -> Outcome:
        analysis = self._task("analyst", "analyze", "reading the codebase")
        optimization = self._task("optimizer", "optimize", "fixing issues", analysis=analysis)
        tests_summary = self._task("tester", "write_tests", "writing tests", analysis=analysis, optimization=optimization)

        rounds: list[str] = []
        round_no, limit, missing_attempts = 1, ROUNDS_PER_BATCH, 0
        while True:
            ok, results, failures = self._verify()
            if ok:
                break
            missing = [c for c in self.components if not results[c.name].has_tests]
            if missing and missing_attempts < 2:
                # Missing tests aren't a code problem: send them straight back to the Test Engineer.
                missing_attempts += 1
                names = ", ".join(c.name for c in missing)
                self.dash.info = f"No tests yet for {names}: back to the Test Engineer"
                tests_summary += "\n\n" + self._task(
                    "tester", "write_missing_tests", f"writing tests for {names}",
                    missing="\n".join(f"- {c.describe()}" for c in missing), analysis=analysis, optimization=optimization)
                continue
            failed = ", ".join(f"{n} ({r.summary})" for n, r in results.items() if not r.ok or not r.has_tests)
            if round_no >= limit:
                choice = choose(
                    f"Tests still fail after {round_no} fix round(s): {failed}",
                    [("more", f"Run {ROUNDS_PER_BATCH} more fix rounds"),
                     ("skip", "Skip the test report and write the QA report anyway (tests still failing)"),
                     ("stop", "Stop here and keep the changes (no reports, no PR)"),
                     ("discard", "Stop and discard all changes")],
                    # Unattended runs must not loop forever: without a terminal, move on to the report.
                    default="more" if interactive() else "skip",
                    suggestions=["Check the failing output in the log; the bug may need a human decision.",
                                 "If the failure needs a service or database, the tests can't pass on this machine."],
                    dashboard=self.dash, log=self.log,
                )
                if choice == "more":
                    limit += ROUNDS_PER_BATCH
                elif choice == "skip":
                    rounds.append(f"Round {round_no}: still failing ({failed}); the user chose to continue without a test report.")
                    return self._finish(analysis, optimization, results, rounds, passed=False, tests_summary=tests_summary)
                else:
                    raise Stop("Stopped with failing tests.", discard=choice == "discard")

            round_no += 1
            self.dash.info = f"Round {round_no}: sending failures back ({failed})"
            changes = "\n".join(f"- {p} ({kind})" for p, kind in self.session.source_changes.items()) or "(none)"
            diagnosis = self._task("analyst", "diagnose", f"diagnosing failures (round {round_no})",
                                   round=round_no, failures=failures, changes=changes)
            fix = self._task("optimizer", "fix_code", f"fixing code (round {round_no})", round=round_no, diagnosis=diagnosis)
            self._task("tester", "repair_tests", f"repairing tests (round {round_no})", round=round_no, diagnosis=diagnosis)
            rounds.append(f"Round {round_no - 1} failed: {failed}. Sent back to the Code Analyst and Optimizer "
                          f"(fix round {round_no}).\nDiagnosis:\n{diagnosis}\nCode fixes:\n{fix}")
            optimization += f"\n\n### Fixes from round {round_no}\n{fix}"

        rounds = rounds or ["All tests passed on the first run."]
        return self._finish(analysis, optimization, results, rounds, passed=True, tests_summary=tests_summary)

    def _finish(self, analysis: str, optimization: str, results: dict[str, TestRun], rounds: list[str],
                passed: bool, tests_summary: str) -> Outcome:
        outcome = Outcome(passed, results, rounds, optimization=optimization)
        final = "\n".join(f"- {name}: {'PASS' if r.ok else 'FAIL'} ({r.summary})" for name, r in results.items())
        rounds_text = "\n\n".join(rounds)

        if passed:  # the test report is only written when every test passes
            report = self._task(
                "tester", "test_report", "writing the test report", final_results=final,
                test_files="\n".join(f"- {f}" for f in sorted(self.session.test_files)) or "(none recorded)",
                rounds=rounds_text, tests_summary=tests_summary,
            )
            outcome.test_report = unique_path(self.repo / TEST_ROOT / f"{self.today}-test-report.md")
            outcome.test_report.write_text(report.strip() + "\n")
            self.dash.activity("Test Engineer", f"saved {outcome.test_report.relative_to(self.repo)}")

        report_dir = self.repo / REPORT_ROOT
        report_dir.mkdir(exist_ok=True)  # if the folder exists, just add the file
        qa = self._task(
            "reporter", "qa_report", "writing the comprehensive report",
            setup=self.setup_summary, analysis=analysis, optimization=optimization,
            changed_files=", ".join(sorted(self.session.source_changes)) or "none",
            final_results=final, rounds=rounds_text,
            test_report_path=str(outcome.test_report.relative_to(self.repo)) if outcome.test_report
            else "not written (tests were still failing)",
            delivery=self.delivery_plan,
        )
        outcome.qa_report = unique_path(report_dir / f"Comprehensive QA report {self.today}.md")
        outcome.qa_report.write_text(qa.strip() + "\n")
        self.dash.activity("QA Reporter", f"saved {outcome.qa_report.relative_to(self.repo)}")
        return outcome


def _error_suggestions(error: Exception) -> list[str]:
    text = f"{type(error).__name__} {error}".lower()
    if "rate" in text and "limit" in text or "429" in text:
        return ["You hit the OpenAI rate limit. Wait a minute, then retry.",
                "For long runs, switch MODEL in .env to a model with higher limits."]
    if "auth" in text or "401" in text or "api key" in text:
        return ["The OpenAI API key was rejected. Fix OPENAI_API_KEY in .env, then retry."]
    if "insufficient_quota" in text or "quota" in text:
        return ["Your OpenAI account is out of credit. Add billing at platform.openai.com, then retry."]
    if "timeout" in text or "connection" in text:
        return ["A network problem reached OpenAI. Check your connection and retry."]
    if "400" in text or "unsupported" in text or "invalid_request" in text:
        return ["This is a configuration error, so retrying will fail the same way.",
                "Choose 'Stop', then try another model: set MODEL in .env (for example MODEL=gpt-5-mini).",
                "If it mentions reasoning_effort, remove or change REASONING_EFFORT in .env."]
    if "context" in text and "length" in text:
        return ["The repository is too large for one prompt. Retry, or test a smaller folder/sub-project."]
    return ["Retrying often works for temporary API errors.", "The full error is in the run log."]
