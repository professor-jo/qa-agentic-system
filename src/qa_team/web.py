"""Local web UI: `qa_team ui` serves a browser front-end for the QA team.

The server only listens on 127.0.0.1. Every API call must carry a random token
that is generated at startup and embedded in the page, and the Host header must
be localhost, so other websites can't drive it. The API key is never sent back
to the browser.
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from qa_team import source
from qa_team.agents import DEFAULT_MODEL, PRICES, model_name
from qa_team.runner import OUTPUT, STAGES, load_history, new_run_dir, run_repo
from qa_team.stack import detect_components
from qa_team.ui import agent_name

STATIC = Path(__file__).parent / "webui"
TOKEN = secrets.token_urlsafe(24)
EFFORTS = ["", "low", "medium", "high", "xhigh"]


# --- one live run: implements the same UI interface as the terminal Dashboard ------

class WebRun:
    interactive = True  # decisions are answered in the browser

    def __init__(self, run_id: str, source_text: str, log):
        self.id, self.source, self.log = run_id, source_text, log
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.stages = {key: {"key": key, "label": label, "status": "pending", "note": "", "started": None, "total": 0.0}
                       for key, label in STAGES}
        self._title, self._info = source_text, ""
        self.pending: dict[str, dict] = {}
        self.result: dict | None = None
        self.finished = False

    def _emit(self, kind: str, **data) -> None:
        with self.cond:
            self.events.append({"type": kind, "time": time.time(), "seq": len(self.events), **data})
            self.cond.notify_all()

    # attributes the runner/pipeline set
    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value
        self._emit("title", text=value)

    @property
    def info(self) -> str:
        return self._info

    @info.setter
    def info(self, value: str) -> None:
        self._info = value
        self._emit("info", text=value)

    # stage updates
    def start(self, key: str, doing: str = "starting") -> None:
        stage = self.stages[key]
        stage.update(status="running", note="", started=time.time())
        self._emit("stage", **stage)
        self.activity(stage["label"], doing)

    def _end(self, key: str, status: str, note: str) -> None:
        stage = self.stages[key]
        if stage["status"] == "running" and stage["started"]:
            stage["total"] += time.time() - stage["started"]
        stage.update(status=status, note=note, started=None)
        self._emit("stage", **stage)

    def done(self, key: str, note: str = "") -> None:
        self._end(key, "done", note)

    def fail(self, key: str, note: str = "") -> None:
        self._end(key, "failed", note)

    def skip(self, key: str, note: str = "") -> None:
        self._end(key, "skipped", note)

    def activity(self, who: str, what: str) -> None:
        who = agent_name(who)
        self.log.write(f"[activity] {time.strftime('%H:%M:%S')}  {who}: {what}\n")
        self.log.flush()
        self._emit("activity", who=who, what=what)

    def finish(self, success: bool, message: str, details: list | None = None) -> None:
        self._emit("finish", success=success, message=message, details=details or [])

    def changes(self, data: dict) -> None:
        self._emit("changes", data=data)

    # decisions: emit a question, block the run thread until the browser answers
    def _ask(self, payload: dict) -> dict:
        decision_id = uuid.uuid4().hex[:12]
        entry = {"event": threading.Event(), "answer": None, "payload": payload}
        self.pending[decision_id] = entry
        self._emit("decision", id=decision_id, **payload)
        entry["event"].wait()
        self.pending.pop(decision_id, None)
        self._emit("decision_resolved", id=decision_id)
        return entry["answer"]

    def decide(self, problem: str, options: list[tuple[str, str]], default: str,
               suggestions: list[str] | None = None) -> str:
        self.log.write(f"[error] {problem}\n")
        answer = self._ask({"problem": problem, "options": [list(o) for o in options], "default": default,
                            "suggestions": suggestions or [], "input": None})
        key = answer.get("key") if answer.get("key") in {k for k, _ in options} else default
        self.log.write(f"[choice] {key}\n")
        return key

    def ask_secret(self, prompt: str) -> str:
        answer = self._ask({"problem": prompt, "options": [["ok", "Save"]], "default": "ok",
                            "suggestions": [], "input": "password"})
        return (answer.get("value") or "").strip()

    def answer(self, decision_id: str, key: str, value: str | None) -> bool:
        entry = self.pending.get(decision_id)
        if not entry:
            return False
        entry["answer"] = {"key": key, "value": value}
        entry["event"].set()
        return True

    def snapshot(self) -> dict:
        return {"id": self.id, "title": self._title, "info": self._info, "done": self.finished,
                "stages": list(self.stages.values()), "result": self.result, "now": time.time()}


RUNS: dict[str, WebRun] = {}
RUN_LOCK = threading.Lock()


def _active() -> WebRun | None:
    return next((r for r in RUNS.values() if not r.finished), None)


def _start_run(source_text: str, branch: str | None, open_pr: bool) -> WebRun:
    run_dir = new_run_dir(source_text)
    run_id = str(run_dir.relative_to(OUTPUT))
    log = open(run_dir / "run.log", "w")
    run = WebRun(run_id, source_text, log)
    RUNS[run_id] = run

    def worker() -> None:
        try:
            result = run_repo(source_text, branch, open_pr, False, run, log, run_dir)
            run.result = asdict(result)
        finally:
            log.close()
            run._emit("done", result=run.result)
            with run.cond:
                run.finished = True
                run.cond.notify_all()

    threading.Thread(target=worker, name=f"qa-run-{run_id}", daemon=True).start()
    return run


# --- helpers ------------------------------------------------------------------------

def _run_record(run_id: str) -> dict:
    """result.json of a finished run (live runs: their result once done)."""
    if run_id in RUNS and RUNS[run_id].result:
        return RUNS[run_id].result
    path = (OUTPUT / run_id / "result.json").resolve()
    if OUTPUT.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "Run not found")
    return json.loads(path.read_text())


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=30)
    return result.stdout if result.returncode == 0 else ""


SKIP_SEARCH = {"Library", "Applications", "Pictures", "Music", "Movies", "node_modules", "site-packages",
               "venv", ".venv", "__pycache__", "build", "dist", "workspace"}


def _find_dirs(name: str, max_depth: int = 5, time_limit: float = 4.0) -> list[Path]:
    """Folders called `name` under the home directory (breadth-first, bounded)."""
    found, queue, deadline = [], [(Path.home(), 0)], time.monotonic() + time_limit
    while queue and time.monotonic() < deadline:
        folder, depth = queue.pop(0)
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False) or entry.name.startswith(".") or entry.name in SKIP_SEARCH:
                        continue
                    if entry.name == name:
                        found.append(Path(entry.path))
                    if depth + 1 < max_depth:
                        queue.append((Path(entry.path), depth + 1))
        except OSError:
            continue
    return found


def _verify_key(key: str) -> str | None:
    """Ask OpenAI whether the key works (listing models is free). Returns a problem, or None."""
    import httpx
    try:
        r = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
    except httpx.HTTPError:
        return "Couldn't reach OpenAI to check the key. Check your internet connection and try again."
    if r.status_code == 401:
        return "OpenAI rejected that key. Copy it again from platform.openai.com/api-keys."
    if r.status_code == 403:
        return "That key doesn't have access to the models. Check its permissions in your OpenAI dashboard."
    return None if r.is_success else f"OpenAI couldn't check the key right now (HTTP {r.status_code}). Try again."


def _env_status() -> dict:
    key = os.getenv("OPENAI_API_KEY", "")
    gh_user = ""
    try:
        gh = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=10)
        gh_user = gh.stdout.strip() if gh.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "has_key": bool(key), "key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
        "model": model_name(), "default_model": DEFAULT_MODEL, "models": list(PRICES),
        "reasoning_effort": os.getenv("REASONING_EFFORT", ""), "efforts": EFFORTS,
        "gh_user": gh_user,
    }


# --- app ---------------------------------------------------------------------------------

def create_app(port: int) -> FastAPI:
    app = FastAPI(title="QA AI Agent System", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            token = request.headers.get("x-qa-token") or request.query_params.get("token")
            if not token or not secrets.compare_digest(token, TOKEN):
                return JSONResponse({"detail": "Missing or invalid token"}, status_code=401)
        return await call_next(request)

    @app.get("/favicon.ico")
    def favicon():
        from fastapi.responses import Response
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#030806"/>'
               '<text x="5" y="22" font-family="monospace" font-size="16" fill="#4ade80">&gt;_</text></svg>')
        return Response(svg, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text().replace("__QA_TOKEN__", TOKEN)

    @app.get("/api/status")
    def status() -> dict:
        active = _active()
        return {**_env_status(), "active_run": active.id if active else None}

    @app.post("/api/settings")
    async def settings(request: Request) -> dict:
        from qa_team.main import save_settings
        body = await request.json()
        values = {}
        if key := (body.get("api_key") or "").strip():
            if not key.startswith("sk-"):
                raise HTTPException(400, "That doesn't look like an OpenAI API key (it should start with sk-).")
            if problem := await asyncio.to_thread(_verify_key, key):
                raise HTTPException(400, problem)
            values["OPENAI_API_KEY"] = key
        if model := (body.get("model") or "").strip():
            values["MODEL"] = model
        if body.get("reasoning_effort") in EFFORTS:
            values["REASONING_EFFORT"] = body["reasoning_effort"]  # "" = the model's default
        if values:
            save_settings(values)
            for key, value in values.items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
        return _env_status()

    @app.get("/api/check")
    def check(src: str) -> dict:
        """Validate a pasted GitHub link or folder path; errors come back as plain messages for a toast."""
        src = source.clean_path(src)
        if not src:
            raise HTTPException(400, "Paste a GitHub link or a folder path first.")
        if re.match(r"^(www\.)?github\.com/", src):
            src = "https://" + src
        if source.REMOTE_URL.match(src):
            out = subprocess.run(["git", "ls-remote", "--symref", src, "HEAD", "refs/heads/*"],
                                 capture_output=True, text=True, timeout=30,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if out.returncode != 0:
                raise HTTPException(400, "Can't access that repository. Check the link, or if it's private, "
                                         "make sure your GitHub account has access.")
            lines = out.stdout.splitlines()
            names = sorted({ln.split("refs/heads/")[1] for ln in lines if "\trefs/heads/" in ln})
            default = next((ln.split("refs/heads/")[1].split()[0] for ln in lines if ln.startswith("ref:")), "")
            label = src.rstrip("/").removesuffix(".git").split("github.com/")[-1]
            return {"source": src, "label": label, "kind": "github", "branches": names, "default": default}
        try:
            path = source.open_local(src)
        except source.SourceError as e:
            message = str(e)
            if "isn't a Git repo" in message:
                message = f"“{Path(src).name}” isn't a Git repository. Run `git init` in it and commit your code first."
            elif "doesn't exist" in message:
                message = "That folder doesn't exist. Check the path."
            raise HTTPException(400, message)
        if not detect_components(path):
            raise HTTPException(400, f"No Python or JavaScript/TypeScript code found in “{path.name}”. "
                                     "This tool supports Python and JS/TS projects.")
        names = sorted(_git(str(path), "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())
        try:
            default = source.current_branch(path)
        except source.SourceError as e:
            raise HTTPException(400, str(e))
        return {"source": str(path), "label": path.name, "kind": "folder", "branches": names, "default": default}

    @app.post("/api/locate")
    async def locate(request: Request) -> dict:
        """Browsers don't reveal where a dropped folder lives, only its name and contents.
        Find the folder under the home directory with that name and the most similar contents."""
        body = await request.json()
        name = (body.get("name") or "").strip()
        entries = set((body.get("entries") or [])[:300])
        if not name or "/" in name:
            raise HTTPException(400, "Drop a single project folder.")
        best, best_score = None, 0.0
        for candidate in await asyncio.to_thread(_find_dirs, name):
            try:
                children = set(os.listdir(candidate))
            except OSError:
                continue
            score = len(entries & children) / max(len(entries), 1)
            if score > best_score or (score == best_score and best and candidate.stat().st_mtime > best.stat().st_mtime):
                best, best_score = candidate, score
        if not best or best_score < 0.6:
            raise HTTPException(404, f"Couldn't find “{name}” on this computer. Paste its folder path instead "
                                     f"(for example ~/projects/{name}).")
        return {"path": str(best)}

    @app.get("/api/browse")
    def browse(path: str = "") -> dict:
        folder = Path(source.clean_path(path) if path else "~").expanduser().resolve()
        if not folder.is_dir():
            raise HTTPException(400, "Folder not found")
        entries = []
        try:
            for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir() and not child.name.startswith("."):
                    entries.append({"name": child.name, "path": str(child), "git": (child / ".git").exists()})
        except PermissionError:
            pass
        return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else None,
                "git": (folder / ".git").exists(), "entries": entries}

    @app.post("/api/runs")
    async def start(request: Request) -> dict:
        body = await request.json()
        text = (body.get("source") or "").strip()
        if not text:
            raise HTTPException(400, "Choose a repository first.")
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(400, "Add your OpenAI API key first (Step 1 at the top of the page).")
        with RUN_LOCK:
            if _active():
                raise HTTPException(409, "A run is already in progress. Wait for it to finish.")
            run = _start_run(text, (body.get("branch") or "").strip() or None, bool(body.get("open_pr", True)))
        return {"id": run.id}

    @app.get("/api/runs")
    def runs() -> dict:
        active = _active()
        return {"active": active.snapshot() if active else None, "history": load_history()}

    @app.get("/api/runs/{run_id:path}/events")
    async def events(run_id: str, request: Request):
        run = RUNS.get(run_id)
        if not run:
            raise HTTPException(404, "Not a live run")
        # After a dropped connection the browser sends the last event id it saw; resume right after it.
        last = request.headers.get("last-event-id", "")
        start = int(last) + 1 if last.isdigit() else 0

        def wait_for(index: int) -> None:
            with run.cond:
                run.cond.wait_for(lambda: len(run.events) > index or run.finished, timeout=15)

        async def stream():
            if start == 0:
                yield f"event: snapshot\ndata: {json.dumps(run.snapshot())}\n\n"
            sent = start
            while True:
                while sent < len(run.events):          # one at a time: events added meanwhile are never skipped
                    event = run.events[sent]
                    yield f"id: {sent}\ndata: {json.dumps(event)}\n\n"
                    sent += 1
                if run.finished and sent >= len(run.events):
                    break
                before = len(run.events)
                await asyncio.to_thread(wait_for, sent)
                if len(run.events) == before and not run.finished:
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id:path}/decision")
    async def decision(run_id: str, request: Request) -> dict:
        run = RUNS.get(run_id)
        body = await request.json()
        if not run or not run.answer(body.get("id", ""), body.get("key", ""), body.get("value")):
            raise HTTPException(404, "No such pending decision")
        return {"ok": True}

    @app.get("/api/runs/{run_id:path}/result")
    def result(run_id: str) -> dict:
        return _run_record(run_id)

    @app.get("/api/runs/{run_id:path}/report")
    def report(run_id: str, which: str = "qa") -> dict:
        record = _run_record(run_id)
        rel = record.get("qa_report" if which == "qa" else "test_report") or ""
        if not rel:
            raise HTTPException(404, "This run has no such report.")
        repo, branch = record["repo_path"], record.get("qa_branch")
        text = _git(repo, "show", f"{branch}:{rel}") if branch else ""
        if not text and (Path(repo) / rel).is_file():
            text = (Path(repo) / rel).read_text()
        if not text:
            raise HTTPException(404, "Report file not found (was the branch deleted?).")
        return {"path": rel, "markdown": text}

    @app.get("/api/runs/{run_id:path}/diff")
    def diff(run_id: str, path: str) -> dict:
        record = _run_record(run_id)
        repo, base, branch = record["repo_path"], record.get("base_branch"), record.get("qa_branch")
        changes = record.get("changes") or {}
        known = {i["path"] for group in ("source", "tests", "reports", "setup") for i in changes.get(group, [])}
        if path not in known:
            raise HTTPException(404, "Not a file changed in this run")
        text = _git(repo, "diff", f"{base}...{branch}", "--", path) if branch else _git(repo, "diff", "HEAD", "--", path)
        return {"path": path, "diff": text or "(no textual changes to show)"}

    return app


def serve(port: int = 8765, open_browser: bool = True) -> None:
    from qa_team.ui import console
    url = f"http://127.0.0.1:{port}/"
    console.print(f"[bold]QA AI Agent System[/] web UI → [cyan]{url}[/]  [dim](local only · Ctrl-C to stop)[/]")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, log_level="warning")
