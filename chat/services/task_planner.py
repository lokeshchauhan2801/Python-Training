"""Multi-task planning for the selected-Agent LangGraph runtime.

One chat message can ask for several things ("give the status of report.pdf,
reprocess it and then make a pie chart of the statuses"). ``plan_tasks`` asks
the LLM to split such a message into ordered, self-contained sub-tasks and to
assign each one to a *worker*: a reachable Step of the selected Agent, which
either runs its own tools or delegates to another Agent (a sub-agent).

``looks_multi_task`` is a cheap lexical gate so a plain single request never
pays for the extra planner call. A plan of one task (or any planner failure)
means the normal single-message pipeline runs unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from django.conf import settings
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

logger = logging.getLogger(__name__)

_I = re.IGNORECASE

# Clause separators: "and", "then", "also", ";", new lines, numbered items.
_CLAUSE_SPLIT = re.compile(
    r"\s*(?:[;\n]+|,\s*(?:and\s+)?(?:then\s+)?|\band\s+then\b|\bafter\s+that\b|"
    r"\bafterwards\b|\bthen\b|\balso\b|\bplus\b|\bas\s+well\s+as\b|"
    r"\b(?:first(?:ly)?|second(?:ly)?|third(?:ly)?|finally|lastly|next)\b|"
    r"\band\b|\b\d+[.)]\s)\s*",
    _I,
)
# A clause counts as a separate request when it carries an action verb.
_ACTION_VERB = re.compile(
    r"\b(give|show|tell|check|get|fetch|find|search|look\s+up|list|report|"
    r"re-?process|process|index|add|save|upload|store|stop|cancel|pause|"
    r"resume|retry|restart|create|make|draw|generate|build|plot|chart|"
    r"visuali[sz]e|summari[sz]e|recap|explain|compare|analy[sz]e|calculate|"
    r"count|write|draft|translate)\b",
    _I,
)

_FALLBACK_PLANNER_PROMPT = (
    "You split a user's chat message into the separate tasks it asks for and "
    "assign each task to the worker that can do it.\n\n"
    "Workers (key: what it can do):\n{workers}\n\n"
    "Files in this chat:\n{files}\n\n"
    "Conversation so far:\n{history}\n\n"
    "User message:\n{question}\n\n"
    "Rules:\n"
    "- Return the tasks in the order the user wants them done.\n"
    "- If the message asks for only ONE thing, return exactly one task.\n"
    "- Rewrite each task as a short, self-contained instruction. Replace "
    "pronouns like 'it' or 'them' with the actual file name, number or subject "
    "(e.g. 'reprocess it' -> 'reprocess report.pdf'). Keep file names and file "
    "numbers exactly as the user wrote them.\n"
    "- worker must be one of the worker keys above.\n"
    "- tool is the one tool slug (listed for that worker) that fits the task, "
    "or empty when unsure.\n"
    "- Set uses_previous to true when the task needs the result of an earlier "
    "task (e.g. 'make a pie chart of those statuses').\n"
    "- title is 2-5 words naming the task (e.g. 'Document status').\n"
    "- Never invent tasks the user did not ask for. At most {max_tasks} tasks."
)


class _PlannedTaskOut(PydanticBaseModel):
    instruction: str = Field(min_length=1, max_length=1000)
    worker: str = ""
    tool: str = ""
    uses_previous: bool = False
    title: str = Field(default="", max_length=80)


class TaskPlan(PydanticBaseModel):
    """The structured output of the LLM task planner."""

    tasks: list[_PlannedTaskOut] = Field(default_factory=list)


@dataclass
class Worker:
    """A reachable Step of the selected Agent that a task can be given to."""

    key: str  # the Step slug
    step: object
    delegate: object | None = None  # the sub-agent the step hands work to
    tools: list = field(default_factory=list)  # tool slugs it can run

    def describe(self) -> str:
        label = self.step.name or self.step.slug
        tools = ", ".join(self.tools) or "general answer only"
        if self.delegate is not None:
            agent_label = self.delegate.name or self.delegate.slug
            return f"{self.key}: step '{label}' run by agent '{agent_label}' (tools: {tools})"
        return f"{self.key}: step '{label}' (tools: {tools})"


@dataclass
class PlannedTask:
    instruction: str
    worker: Worker
    tool: str = ""
    uses_previous: bool = False
    title: str = ""


def enabled() -> bool:
    return bool(getattr(settings, "CHAT_MULTI_TASK_ENABLED", True))


def max_tasks() -> int:
    return max(int(getattr(settings, "CHAT_MULTI_TASK_MAX", 5)), 2)


def looks_multi_task(message_text) -> bool:
    """True when the message seems to ask for two or more separate actions."""
    text = (message_text or "").strip()
    if not enabled() or len(text.split()) < 4:
        return False
    clauses = [c for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]
    return sum(1 for c in clauses if _ACTION_VERB.search(c)) >= 2


def plan_tasks(
    *, llm, langfuse, message_text, history_text, workers, file_titles=()
) -> list[PlannedTask]:
    """Split ``message_text`` into ordered tasks, or ``[]`` for a single task.

    Unknown worker keys fall back to the worker that has the named tool (else
    the first worker). Only an ``LLMConfigurationError`` is raised; any other
    planner failure returns ``[]`` so the normal pipeline answers the message.
    """
    from basics.llm import LLMConfigurationError

    if not workers:
        return []
    limit = max_tasks()
    files = "\n".join(f"- {t}" for t in file_titles if t) or "(none)"
    fmt = {
        "workers": "\n".join(f"- {w.describe()}" for w in workers),
        "files": files,
        "history": history_text or "(no prior conversation)",
        "question": message_text,
        "max_tasks": limit,
    }
    template = (
        langfuse.get_prompt("agent_task_planner", fallback=_FALLBACK_PLANNER_PROMPT)
        if langfuse
        else _FALLBACK_PLANNER_PROMPT
    )
    try:
        prompt = template.format(**fmt)
    except (KeyError, IndexError):
        prompt = _FALLBACK_PLANNER_PROMPT.format(**fmt)

    try:
        if langfuse:
            with langfuse.trace("agent_task_planner"):
                plan = llm.generate_structured(prompt, TaskPlan)
        else:
            plan = llm.generate_structured(prompt, TaskPlan)
    except LLMConfigurationError:
        raise
    except Exception:  # noqa: BLE001 - a failed plan falls back to one task
        logger.warning("Task planner failed; running the message as one task", exc_info=True)
        return []

    by_key = {w.key: w for w in workers}
    tasks: list[PlannedTask] = []
    for item in plan.tasks[:limit]:
        instruction = item.instruction.strip()
        if not instruction:
            continue
        tool = (item.tool or "").strip()
        worker = by_key.get((item.worker or "").strip())
        if worker is None or (tool and tool not in worker.tools):
            worker = next((w for w in workers if tool and tool in w.tools), None) or (
                worker or workers[0]
            )
        tasks.append(
            PlannedTask(
                instruction=instruction,
                worker=worker,
                tool=tool if tool in worker.tools else "",
                uses_previous=bool(item.uses_previous) and bool(tasks),
                title=(item.title or "").strip(),
            )
        )
    return tasks if len(tasks) > 1 else []
