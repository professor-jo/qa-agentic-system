# Agentic QA

An AI QA team for your repositories: on GitHub, in local folders, or someone
else's project you've cloned. It analyzes the code, fixes and optimizes it,
writes and runs a test suite, and documents everything. The work is delivered
on its own branch with a pull request.

| # | Agent | What it does |
|---|-------|--------------|
| 1 | **Code Analyst** | Reads the whole project and reports its architecture, issues and what to test |
| 2 | **Optimizer** | **Fixes** bugs and inefficiencies first, then recommends UI/UX and framework/library improvements |
| 3 | **Test Engineer** | Writes tests in `agentic test/` and runs them. If any fail, they go **back to agents 1 and 2** to fix, until all pass. Then it writes `agentic test/yyyy-mm-dd-test-report.md` |
| 4 | **QA Reporter** | Writes `Agentic QA Report/Comprehensive QA report yyyy-mm-dd.md`, covering the whole process |

Supports **Python** (pytest) and **JavaScript/TypeScript** (vitest, with React
Testing Library for UI code), including repos that have both, such as a Python
backend with a React frontend.

## Requirements

| Tool | Needed for | Install (macOS) |
|------|------------|-----------------|
| **Python 3.11–3.13** | running the app | `brew install python@3.12` or [uv](https://docs.astral.sh/uv/) |
| **Git** | everything | `xcode-select --install` |
| **Node.js 18+** | testing JavaScript/TypeScript projects | `brew install node` |
| **GitHub CLI** (optional) | listing your repos, opening PRs | `brew install gh`, then `gh auth login` |
| **OpenAI API key** | the agents | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |

## Setup

```bash
git clone https://github.com/professor-jo/qa-agentic-system.git
cd qa-agentic-system

# create a virtual environment (Python 3.11–3.13) and install
python3.12 -m venv .venv            # or: uv venv --python 3.12
source .venv/bin/activate
pip install -e .                    # or: uv pip install -e .
```

### Your API key

You bring your own OpenAI API key. Runs are billed to your OpenAI account, and
each run shows its token usage and estimated cost at the end.

- **Easiest:** just run `qa_team`. It asks for your key (input is hidden) and
  offers to save it to a local `.env` file.
- **Or by hand:** `cp .env.example .env` and paste your key into `.env`.

`.env` is listed in `.gitignore`, so your key stays on your machine and is
never committed. Set `MODEL` in `.env` to change the model (default
`gpt-6.1-sol`; `gpt-5-mini` is the budget option).

## Run

```bash
source .venv/bin/activate                     # once per terminal window
qa_team                                       # asks for a repo; Enter lists your GitHub repos
qa_team https://github.com/owner/repo         # any GitHub repo you can read
qa_team ~/code/my-project --branch dev        # a local Git repo
qa_team --mine                                # pick several of your GitHub repos (e.g. 1,3-5 or all)
```

| Flag | Meaning |
|------|---------|
| `--branch NAME` | Branch to QA (default: the current/default branch) |
| `--no-pr` | Commit to a local branch only; never push |
| `-v`, `--verbose` | Show the agents' raw output instead of the dashboard |

### What you see

One live dashboard per repository shows each agent's status, what it's doing
right now, and a short activity feed:

```
╭─ Agentic QA · https://github.com/owner/repo @ main ───────────────────────╮
│ ✓ Setup             backend (python), frontend (js) · 1m 12s              │
│ ✓ 1 · Code Analyst  1m 40s                                                │
│ ⠋ 2 · Optimizer     editing backend/app/services/llm.py · 52s             │
│ · 3 · Test Engineer                                                       │
│ · 4 · QA Reporter                                                         │
│ · Branch & PR                                                             │
│ ── activity ───────────────────────────────────────────────────────────── │
│ 14:02:11  Optimizer: reading backend/app/services/llm.py                  │
│ 14:02:19  Optimizer: editing backend/app/services/llm.py                  │
╰───────────────────────────────────────────────────────────────────────────╯
```

When something goes wrong (an install fails, the API errors, tests keep
failing, you have no push access), it logs the error, suggests fixes, and asks
you to pick an option, for example:

```
╭─ Needs your decision ─────────────────────────────────────────────────────╮
│ Tests still fail after 3 fix round(s): backend (2 failed, 40 passed)       │
│   1) Run 3 more fix rounds (default)                                       │
│   2) Skip the test report and write the QA report anyway                   │
│   3) Stop here and keep the changes (no reports, no PR)                    │
│   4) Stop and discard all changes                                          │
╰───────────────────────────────────────────────────────────────────────────╯
```

## What ends up in the repository

On a new branch `qa/agentic-YYYYMMDD-HHMMSS`:

```
<repo>/
├── …source files fixed by the Optimizer
├── agentic test/
│   ├── backend/   unit/ integration/ …       ← tests per component
│   ├── frontend/  components/ unit/ …  (+ vitest config)
│   └── 2026-10-15-test-report.md             ← only written when all tests pass
└── Agentic QA Report/
    └── Comprehensive QA report 2026-10-15.md
```

- **With write access:** the branch is pushed and a PR is opened into the
  branch that was tested.
- **Without write access** (someone else's repo): you choose between forking
  and opening a PR upstream, or keeping the branch locally.
- **Your branch is never modified directly.** After the run, the checkout
  returns to the branch you tested.

## Safety

- **Edit permissions are per agent.** The Optimizer can edit source but not
  tests, lockfiles, `.env*` or dependencies. The Test Engineer can only write
  inside `agentic test/`.
- **Nothing in the project's setup is touched.** Test tooling (vitest etc.) is
  installed inside `agentic test/<component>/node_modules` (git-ignored), and
  Python virtualenvs live outside the repo in `workspace/.venvs/`. The
  project's `package.json`, lockfiles and requirements stay as they are.
- **Uncommitted work in a local repo** can be stashed during the run and
  restored afterwards.
- **"Discard" restores every file the agents touched** to its exact original
  contents.
- **Logs:** every run writes a full log to `output/<repo>/<timestamp>/run.log`.
- **Generated tests execute the repo's code** on your machine, so only run
  this on code you trust.

See [docs/WORKFLOW.md](docs/WORKFLOW.md) for the detailed flow.

## Layout

```
src/qa_team/
  main.py       CLI, repo selection, setup, per-repo dashboard
  source.py     GitHub URL / local path / your-repos picker → checkout
  stack.py      detect Python & JS parts, install deps, build the `agentic test/` harness, run tests
  agents.py     builds and runs the CrewAI agents from config/*.yaml
  pipeline.py   analyze → optimize → test → (fail → diagnose → fix → retest) → reports
  tools.py      the agents' tools and their permissions
  delivery.py   branch, commit, push, PR (or fork + PR)
  ui.py         live dashboard and decision prompts
  config/       agent roles and task prompts
```

## License

[MIT](LICENSE)
