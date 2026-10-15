# Agentic QA Workflow

This document walks through what happens when you run `qa_team` on a
repository. Usage is covered in the [README](../README.md).

```mermaid
flowchart TD
    IN([GitHub URL · local folder · pick from your GitHub]) --> S[Setup]
    S -- not accessible --> MSG([Message: not an accessible repository])
    S --> A[1 · Code Analyst<br/>reads the whole project]
    A --> O[2 · Optimizer<br/>fixes first, then recommendations]
    O --> T[3 · Test Engineer<br/>writes tests in 'agentic test/', runs them]
    T --> V{Independent test run:<br/>all pass?}
    V -- no --> D[Code Analyst diagnoses<br/>CODE BUG / TEST BUG] --> F[Optimizer fixes code] --> R[Test Engineer repairs tests] --> V
    V -- "no, after 3 rounds" --> ASK{{You choose: more rounds /<br/>skip test report / stop / discard}}
    V -- yes --> TR[Test report<br/>agentic test/yyyy-mm-dd-test-report.md]
    TR --> Q[4 · QA Reporter<br/>Agentic QA Report/Comprehensive QA report yyyy-mm-dd.md]
    Q --> B[Commit on qa/agentic-… branch]
    B --> P{Write access?}
    P -- yes --> PR([Push + PR into the tested branch])
    P -- no --> FK{{You choose: fork + PR upstream / keep local branch}}
```

## Setup

1. **Get the repository.**
   - GitHub URLs are cloned fresh into `workspace/<repo>`. If an earlier clone
     holds QA branches that were never pushed, you're asked before it's replaced.
   - Local folders are used in place. If one has uncommitted changes, you can
     stash them for the run (they're restored at the end), continue, or cancel.
   - Anything that isn't an accessible Git repository stops with a message
     explaining why.
2. **Detect components.** Every folder (up to 3 levels deep) that contains
   `pyproject.toml`, `setup.py`, `setup.cfg` or `requirements.txt` is a Python
   component, and every folder with a real `package.json` is a JS/TS component.
   Each one gets its own test folder: `agentic test/<component>/`.
3. **Install dependencies.**
   - **Python:** a virtualenv in `workspace/.venvs/<repo>/<component>`, with
     the project, its test/dev extras, its `requirements*.txt` and pytest. The
     project's own tests run once to get a baseline.
   - **JS/TS:**
     - The project's dependencies are installed with `npm ci`, or
       `npm install --no-save`. Neither changes the lockfile.
     - A small harness is generated in `agentic test/<component>/`:
       `package.json`, `vitest.config.mjs`, a setup file and `.gitignore`.
     - vitest (plus jsdom and Testing Library for UI projects) is installed
       there, and the project's own React is linked in so there's only one copy.
   - If an install fails, you choose to skip that component, retry or stop.
4. **Check the OpenAI key and model** with one tiny call. If it fails, you
   choose to retry, enter a new key or stop.

## The agents

| Agent | Tools | Task | Output |
|-------|-------|------|--------|
| 1 · Code Analyst | list, read, search | Reads the whole project. Reports the overview, architecture, code quality issues (`path:line`), dated patterns and what to test. | analysis (passed on) |
| 2 · Optimizer | list, read, search, **edit**, create | **Priority 1:** fixes bugs, mistakes and inefficiencies with surgical edits. **Priority 2:** recommends UI/UX and framework/library upgrades (not applied). | changes table + recommendations |
| 3 · Test Engineer | list, read, search, **write test**, run tests | Writes tests in `agentic test/<component>/` (subfolders such as `unit/`, `integration/`, `components/`), covering the analysis and every fix, and runs them. | test files + summary |
| 4 · QA Reporter | none | Writes the comprehensive report from the team's records. | `Comprehensive QA report yyyy-mm-dd.md` |

### The failure loop

After the Test Engineer finishes, the program itself runs every component's
tests. It also re-runs the project's own Python tests if they passed before,
which catches regressions the Optimizer introduced.

If anything fails, the work goes back to the first two agents:

1. The **Code Analyst** diagnoses each failure as a *CODE BUG* or a *TEST BUG*,
   giving the root cause and the fix.
2. The **Optimizer** fixes the CODE BUGs.
3. The **Test Engineer** repairs the TEST BUGs (it may never weaken a correct
   assertion), and everything runs again.

After 3 rounds, you're asked: run more rounds, skip the test report and write
the QA report anyway, stop and keep the changes, or discard everything.

### Reports

- **`agentic test/yyyy-mm-dd-test-report.md`** is written **only when every
  test passes**. It's short: a verdict, a results table, what's covered, and
  the bugs that were caught and fixed.
- **`Agentic QA Report/Comprehensive QA report yyyy-mm-dd.md`** covers the
  whole process: summary, process table, analysis, fixes, recommendations,
  testing rounds and delivery. If the folder exists, only the file is added.

If a report with the same name already exists (a second run the same day),
` (2)` is appended to the new file's name.

## Delivery

1. **Commit** on a new branch `qa/agentic-YYYYMMDD-HHMMSS`. Only the agents'
   work is committed: the changed source files, `agentic test/` (without
   `node_modules`) and `Agentic QA Report/`.
2. **Push and open a PR** into the tested branch, if the active `gh` account
   has push access.
3. **Without push access,** you choose to fork and open a PR upstream (useful
   for helping other developers) or keep the branch locally.
4. **Switch back** to the tested branch. With `--no-pr`, the work is only
   committed locally.

## Errors

Every error is written to `output/<repo>/<timestamp>/run.log`, together with the
suggestions shown and the option you chose. Without a terminal (CI, piped input),
the default option is used automatically and logged.

| Situation | Options |
|-----------|---------|
| Old clone has unpushed QA work | move it aside · delete it |
| Local repo has uncommitted changes | stash during the run · continue · cancel |
| Dependency install fails | skip the component · retry · stop |
| OpenAI key or model rejected | retry · enter a new key · stop |
| An agent errors (rate limit, outage, …) | retry · stop and keep changes · stop and discard |
| Tests still failing after N rounds | more rounds · skip the test report · stop · discard |
| No push access | fork + PR · keep the branch locally |

## Limits

- Python and JavaScript/TypeScript only.
- Tests that need real services (databases, external APIs) must mock them,
  because nothing is started for them.
- Only the project's own **Python** tests are re-run for regressions. JS
  projects' own test scripts (for example Playwright end-to-end tests) are not run.
