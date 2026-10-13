import os
from pathlib import Path
from typing import Callable

from crewai import LLM, Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from qa_team.repo_env import RepoEnv
from qa_team.tools.repo_tools import (
    ListFilesTool,
    ReadFileTool,
    RunTestsTool,
    WriteTestFileTool,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "gpt-5-mini"
# Written by QATeam.save_reports. Not via Task.output_file, because CrewAI
# strips the leading "/" from absolute paths and writes them relative to the cwd.
REPORT_FILES = {
    "analyze_code_task": "ANALYSIS.md",
    "find_issues_task": "FINDINGS.md",
    "write_and_run_tests_task": "TEST_REPORT.md",
}


def build_llm() -> LLM:
    # Reasoning models (gpt-5*, o-series) reject any temperature except the
    # default, so only send one when the user explicitly sets TEMPERATURE.
    kwargs = {}
    if temperature := os.getenv("TEMPERATURE"):
        kwargs["temperature"] = float(temperature)
    return LLM(model=os.getenv("MODEL", DEFAULT_MODEL), **kwargs)


@CrewBase
class QATeam:
    """Sequential QA crew: analyze code -> find bugs & optimizations -> write, run & report tests."""

    agents: list[BaseAgent]
    tasks: list[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(
        self,
        repo_path: str,
        env: RepoEnv,
        verbose: bool = False,
        on_task_done: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_path = str(Path(repo_path).resolve())
        self.env = env
        self.verbose = verbose
        self.on_task_done = on_task_done
        self.output_dir = PROJECT_ROOT / "output" / Path(self.repo_path).name
        self.llm = build_llm()
        # Kept so the caller knows exactly which files the crew created (and should commit).
        self.write_tool = WriteTestFileTool(repo_root=self.repo_path, tests_dir=env.tests_dir)

    def _read_tools(self) -> list:
        return [ListFilesTool(repo_root=self.repo_path), ReadFileTool(repo_root=self.repo_path)]

    def _task(self, name: str) -> Task:
        callback = (lambda _output: self.on_task_done(name)) if self.on_task_done else None
        return Task(config=self.tasks_config[name], callback=callback)  # type: ignore[index]

    @agent
    def code_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["code_analyst"],  # type: ignore[index]
            tools=self._read_tools(),
            llm=self.llm,
            max_iter=25,
            verbose=self.verbose,
        )

    @agent
    def bug_hunter(self) -> Agent:
        return Agent(
            config=self.agents_config["bug_hunter"],  # type: ignore[index]
            tools=self._read_tools(),
            llm=self.llm,
            max_iter=25,
            verbose=self.verbose,
        )

    @agent
    def test_engineer(self) -> Agent:
        return Agent(
            config=self.agents_config["test_engineer"],  # type: ignore[index]
            tools=[
                *self._read_tools(),
                self.write_tool,
                RunTestsTool(
                    repo_root=self.repo_path,
                    tests_dir=self.env.tests_dir,
                    project_dir=self.env.project_dir,
                    python=self.env.python,
                ),
            ],
            llm=self.llm,
            max_iter=40,
            verbose=self.verbose,
        )

    @task
    def analyze_code_task(self) -> Task:
        return self._task("analyze_code_task")

    @task
    def find_issues_task(self) -> Task:
        return self._task("find_issues_task")

    @task
    def write_and_run_tests_task(self) -> Task:
        return self._task("write_and_run_tests_task")

    def save_reports(self, crew: Crew) -> dict[str, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        for crew_task in crew.tasks:
            filename = REPORT_FILES.get(crew_task.name or "")
            if filename and crew_task.output is not None:
                path = self.output_dir / filename
                path.write_text(crew_task.output.raw.strip() + "\n")
                written[filename] = path
        return written

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=self.verbose,
        )
