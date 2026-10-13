# agentic-ai-system

A CrewAI **QA team** for Python repositories. Give it a GitHub URL or a local
path and it will:

1. **Analyze** the code
2. **Find** bugs and optimization opportunities
3. **Write, run and report on** a pytest suite
4. **Open a pull request** with the new tests, if you have write access to the repo

| Step | Who | Output |
|------|-----|--------|
| 1 | Code Analyst agent | `ANALYSIS.md` |
| 2 | Bug & Performance Reviewer agent | `FINDINGS.md` |
| 3 | Test Engineer agent | new test files + `TEST_REPORT.md` |
| 4 | plain code (`gh` CLI) | a PR on branch `qa/tests-…` |

Reports are written to `output/<repo-name>/`. For a detailed walkthrough of
each stage, see [docs/WORKFLOW.md](docs/WORKFLOW.md).

## Setup

```bash
uv pip install --python .venv/bin/python -e .
cp .env.example .env   # then set OPENAI_API_KEY (and optionally MODEL)
gh auth login          # only needed for step 4 (pull requests)
```

## Run

```bash
source .venv/bin/activate
qa_team
```

It asks for a repository and a branch, then shows one line per stage:

```
[1/6] Getting https://github.com/owner/repo ... on branch main (2s)
[2/6] Installing dependencies ... existing tests: 96 passed (21s)
[3/6] Analyzing the code ... done (1m 05s)
[4/6] Looking for bugs and optimizations ... done (1m 40s)
[5/6] Writing and running tests ... done (3m 12s)
[6/6] Creating a pull request ... skipped (6s)

SUCCESS  https://github.com/owner/repo @ main
  All tests pass.
  Tests:    118 passed  (before: 96 passed; 2 new test file(s))
  PR:       skipped: you have no write access to owner/repo
  Reports:  output/repo/
```

The result is **SUCCESS** only if a final, independent pytest run passes. If
tests fail, it's **FAIL**, which often means the new tests reproduced real bugs
(see `TEST_REPORT.md`).

You can also pass everything on the command line. The exit code is 0 on success
and 1 on failure:

```bash
qa_team https://github.com/owner/repo                  # default branch
qa_team https://github.com/owner/repo --branch dev     # a specific branch
qa_team ~/code/my-project                              # a local repo
qa_team https://github.com/owner/repo --no-pr          # never push / open a PR
qa_team https://github.com/owner/repo --setup-only     # just install deps + run existing tests (no API key)
qa_team ... -v                                         # show the agents' full output live
```

Without `-v`, the agents' full output is saved to `output/<repo-name>/run.log`.

## Safety

- **Source code is never modified.** The agents can only create *new* test
  files. Existing files, including existing tests and `conftest.py`, can't be
  changed.
- **Pull requests contain only the new test files.** They're opened from a new
  `qa/tests-…` branch into the branch you tested, and are never merged
  automatically. A PR is only attempted if the active `gh` account has push
  access; otherwise the step is skipped and the reason is shown.
- **Dependencies are installed in isolation,** in a virtualenv at
  `workspace/.venvs/<repo>`. Only Python repos are supported.
- **With a local path,** `--branch` checks out that branch in your repo, and
  step 4 commits to a new branch there.
- **Generated tests run the repo's code on your machine.** Only run this on
  repositories you trust.

## Layout

```
src/qa_team/
  main.py              # CLI, progress output and SUCCESS/FAIL result
  repo_source.py       # URL / PR / path -> local checkout
  repo_env.py          # find tests, build virtualenv, install dependencies
  crew.py              # the three agents and their tasks
  publish.py           # step 4: write-access check, commit, push, gh pr create
  config/agents.yaml   # roles, goals and backstories
  config/tasks.yaml    # task prompts and expected outputs
  tools/repo_tools.py  # list/read files, write new tests, run pytest
```
