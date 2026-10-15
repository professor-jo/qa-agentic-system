# Working on this repository

This repo is **public**. Anyone can clone `main` and use it with their own LLM
API key. **Every branch of a public repo is public too**, `dev` included.

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Public release. Must always install and run for a stranger on a fresh clone. |
| `dev`  | Day-to-day work: updates, experiments, trial and error. **Work here by default.** |

## Rules for AI assistants (and humans)

1. **Never push to `main`, merge into `main`, or open-and-merge a PR into
   `main` without the owner's explicit confirmation in the current session.**
   A general "push it" means push `dev`. If it's unclear which branch is
   meant, ask.
2. **Before anything goes to `main`, show the owner what will change**
   (`git log --oneline main..dev` and `git diff --stat main..dev`), run the
   release checklist below, and wait for a yes.
3. **Never commit secrets.** API keys live only in the local `.env` file,
   which is git-ignored. Never print a key, never `git add -f .env`, and never
   put a key in code, docs, tests or commit messages. The owner's local
   pre-commit hook blocks `sk-…` keys and `.env` files; don't bypass it with
   `--no-verify`.
4. **Don't change the repo's visibility, default branch, or branch protection
   settings** without being asked.

## Release checklist (dev → main)

- [ ] `git log -p main..dev | grep -E 'sk-[A-Za-z0-9_-]{20,}'` finds nothing,
      and no `.env` file is tracked (`git ls-files | grep -E '(^|/)\.env$'` is empty).
- [ ] No personal paths, usernames or test data from the owner's machine are in
      tracked files (`git ls-files | xargs grep -n '/Users/'`).
- [ ] `python -m pyflakes src` is clean.
- [ ] A fresh clone installs and runs with **no key present**:
      `git clone <repo> /tmp/x && cd /tmp/x && python3.12 -m venv .venv && . .venv/bin/activate && pip install -e . && qa_team --help`
- [ ] README and `docs/WORKFLOW.md` match the behaviour.
- [ ] The owner said yes. Then: `git checkout main && git merge --ff-only dev && git push && git checkout dev`.

## Project notes

- Entry point: `qa_team` (`src/qa_team/main.py`). The pipeline is described in `docs/WORKFLOW.md`.
- The model is set by `MODEL` in `.env` (default in `src/qa_team/agents.py`).
- `workspace/` (cloned repos) and `output/` (run logs) are local only and git-ignored.
