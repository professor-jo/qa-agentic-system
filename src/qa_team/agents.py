"""Build and run single-task CrewAI crews from the prompts in config/.

The pipeline calls agents one task at a time (instead of one sequential crew)
because failing tests have to loop back to earlier agents.
"""

import contextlib
import os
from functools import cache
from pathlib import Path

import yaml
from crewai import LLM, Agent, Crew, Process, Task

from qa_team.tools import Session, optimizer_tools, read_tools, tester_tools

CONFIG = Path(__file__).parent / "config"
DEFAULT_MODEL = "gpt-5.4-mini"
# USD per 1M tokens (input, cached input, output), Standard tier, from
# developers.openai.com/api/docs/pricing (checked 2026-10-15). Used only for the cost estimate.
PRICES = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
}
MAX_ITER = {"analyst": 30, "optimizer": 40, "tester": 50, "reporter": 5}
# Tool-step budgets per task, shown to the agent. Fix rounds get tight budgets: the harness
# already hands them the failing tests and the relevant source lines.
TASK_STEPS = {"analyze": 30, "optimize": 40, "write_tests": 50, "write_missing_tests": 30,
              "diagnose": 12, "fix_code": 18, "repair_tests": 22, "quarantine_tests": 12,
              "test_report": 3, "qa_report": 3}
LABELS = {"analyst": "Code Analyst", "optimizer": "Optimizer", "tester": "Test Engineer", "reporter": "QA Reporter"}


class _Blank(dict):
    def __missing__(self, key: str) -> str:  # unknown placeholders stay visible instead of crashing
        return "{" + key + "}"


@cache
def _config(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def build_llm() -> LLM:
    # The Responses API: on Chat Completions, newer models (e.g. gpt-6.1-sol) reject
    # tool calls combined with reasoning, and CrewAI's fallback (reasoning_effort="none")
    # is rejected too. Responses supports both.
    kwargs: dict = {"api": "responses"}
    # gpt-5* / o-series reject any temperature but the default; only send one if set.
    if temperature := os.getenv("TEMPERATURE"):
        kwargs["temperature"] = float(temperature)
    if effort := os.getenv("REASONING_EFFORT"):  # low | medium | high | xhigh; unset = model default
        kwargs["reasoning_effort"] = effort
    return LLM(model=model_name(), **kwargs)


def model_name() -> str:
    return os.getenv("MODEL", DEFAULT_MODEL)


def usage_summary(usage: dict[str, int]) -> str:
    """'1.9M in (1.6M cached) / 120k out · ≈ $1.84 (gpt-6.1-sol)'."""
    def short(n: int) -> str:
        return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
    text = f"{short(usage['prompt'])} in ({short(usage['cached'])} cached) / {short(usage['completion'])} out"
    if price := PRICES.get(model_name()):
        uncached = usage["prompt"] - usage["cached"]
        cost = (uncached * price[0] + usage["cached"] * price[1] + usage["completion"] * price[2]) / 1e6
        text += f" · ≈ ${cost:.2f}"
    return f"{text} ({model_name()})"


def check_llm() -> str | None:
    """A tiny agent run with one tool call, so a bad key, an unknown model, or a model
    that can't use tools fails fast with a clear message (the agents rely on tools)."""
    from crewai.tools import tool

    @tool("add_numbers")
    def add_numbers(a: int, b: int) -> str:
        """Add two integers."""
        return str(a + b)

    llm = build_llm()
    try:
        with open(os.devnull, "w") as devnull, contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            agent = Agent(role="Checker", goal="Use the tool", backstory="You always use your tools.",
                          tools=[add_numbers], llm=llm, max_iter=3, verbose=False)
            task = Task(description="Use add_numbers to add 2 and 3, then reply with just the result.",
                        expected_output="a number", agent=agent)
            Crew(agents=[agent], tasks=[task], verbose=False).kickoff()
    except Exception as e:
        return f"{llm.model}: {type(e).__name__}: {str(e)[:300]}"
    return None


def _tools_for(agent_key: str, session: Session) -> list:
    return {
        "analyst": read_tools,
        "optimizer": optimizer_tools,
        "tester": tester_tools,
        "reporter": lambda s: [],
    }[agent_key](session)


def run_task(task_key: str, session: Session, inputs: dict, log, verbose: bool = False) -> str:
    """Run one task with its agent and return the agent's final answer."""
    task_cfg = _config("tasks.yaml")[task_key]
    agent_key = task_cfg["agent"]
    agent_cfg = _config("agents.yaml")[agent_key]
    values = _Blank({k: str(v) for k, v in inputs.items()})

    session.agent = LABELS[agent_key]
    steps = TASK_STEPS.get(task_key, MAX_ITER[agent_key])
    session.begin_task(steps)
    session.activity("thinking")
    agent = Agent(
        role=agent_cfg["role"], goal=agent_cfg["goal"].strip(), backstory=agent_cfg["backstory"].strip(),
        tools=_tools_for(agent_key, session), llm=build_llm(), max_iter=steps + 2,
        allow_delegation=False, verbose=verbose,
    )
    task = Task(
        description=task_cfg["description"].format_map(values),
        expected_output=task_cfg["expected_output"].format_map(values),
        agent=agent,
    )
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=verbose)
    log.write(f"\n===== {LABELS[agent_key]}: {task_key} =====\n")
    log.flush()
    with contextlib.ExitStack() as stack:
        if not verbose:
            stack.enter_context(contextlib.redirect_stdout(log))
            stack.enter_context(contextlib.redirect_stderr(log))
        result = crew.kickoff()  # no inputs: prompts are already filled in, braces in code stay intact
    output = result.raw.strip()
    if metrics := crew.usage_metrics:
        session.usage["prompt"] += metrics.prompt_tokens
        session.usage["cached"] += metrics.cached_prompt_tokens
        session.usage["completion"] += metrics.completion_tokens
        log.write(f"[usage] {task_key}: {metrics.prompt_tokens} in ({metrics.cached_prompt_tokens} cached), "
                  f"{metrics.completion_tokens} out; run total: {usage_summary(session.usage)}\n")
    log.write(f"\n----- result -----\n{output}\n")
    log.flush()
    return output
