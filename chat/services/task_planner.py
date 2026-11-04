"""The supervisor: decides which worker handles each part of a message.

An Agent is a supervisor over its workers (its Steps). ``plan_tasks`` asks the
LLM to split a message ("what is India's GDP, summarise the chat, make a pie
chart of it and add this file to the knowledge base") into parts and to give
each part to the worker that fits: a worker runs its own tools, hands the part
to another Agent, or (no tools) answers with the LLM itself. Each part names
the earlier parts whose ANSWER it needs (``depends_on``); parts that need
nothing from each other run at the same time.

``looks_multi_task`` is a cheap lexical gate so an agent with a single worker
does not pay for a planner call on a plain single request.
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
    "You are the supervisor of a team of agents. Split the user's chat "
    "message into the separate parts it asks for and give each part to the "
    "worker that should do it.\n\n"
    "Workers (key: who they are and what they can do):\n{workers}\n\n"
    "When each tool fits:\n{rules}\n\n"
    "Files in this chat:\n{files}\n\n"
    "Conversation so far:\n{history}\n\n"
    "User message:\n{question}\n\n"
    "Rules:\n"
    "- Return the parts in the order the user asked for them.\n"
    "- If the message asks for only ONE thing, return exactly one part, "
    "given to the worker that fits it best.\n"
    "- Pick the worker by what it does (its description and tools). A worker "
    "with no tools answers with its own knowledge in its role, so give it the "
    "parts its description covers.\n"
    "- Rewrite each part as a short, self-contained instruction. Replace "
    "pronouns like 'it' or 'them' with the actual file name, number or subject "
    "(e.g. 'reprocess it' -> 'reprocess report.pdf'). Keep file names and file "
    "numbers exactly as the user wrote them.\n"
    "- worker must be one of the worker keys above.\n"
    "- tool is the one tool slug (listed for that worker) that fits the part, "
    "or empty when unsure or when the worker has no tools.\n"
    "- depends_on lists the numbers (1 = first part) of the EARLIER parts "
    "whose answer this part needs, e.g. 'make a pie chart of those GDP "
    "figures' depends on the part that gives the figures. Leave it empty when "
    "the part needs nothing from the others: such parts run at the same time. "
    "A part that reads a file itself, a chat summary, or adding a file to the "
    "knowledge base usually depends on nothing.\n"
    "- When the user wants results written up, emailed, translated, turned "
    "into action items or charted, first add the part that GETS the results "
    "(e.g. a document search), then the writing part depending on it.\n"
    "- ONE action on several files is ONE part: keep 'process file 1 and 2' "
    "or 'stop a.pdf and b.pdf' together, with the files exactly as written.\n"
    "- title is 2-5 words naming the part (e.g. 'Document status').\n"
    "- Never invent parts the user did not ask for. At most {max_tasks} parts."
)


# Tools whose whole job is to work on an earlier part's answer: a part using
# one depends on the part before it when the supervisor named no dependency.
EARLIER_OUTPUT_TOOLS = frozenset(
    {"report_writer", "action_items", "email_drafter", "translator", "chart_builder"}
)


class _PlannedTaskOut(PydanticBaseModel):
    instruction: str = Field(min_length=1, max_length=1000)
    worker: str = ""
    tool: str = ""
    depends_on: list[int] = Field(default_factory=list)
    title: str = Field(default="", max_length=80)


class TaskPlan(PydanticBaseModel):
    """The structured output of the supervisor."""

    tasks: list[_PlannedTaskOut] = Field(default_factory=list)


@dataclass
class Worker:
    """One worker (Step) of an Agent that the supervisor can give a part to."""

    key: str  # the Step slug
    step: object
    delegate: object | None = None  # the agent the step hands work to
    tools: list = field(default_factory=list)  # tool slugs it can run

    @property
    def name(self) -> str:
        owner = self.delegate or self.step
        return owner.name or owner.slug

    def describe(self) -> str:
        label = self.step.name or self.step.slug
        about = ""
        owner = self.delegate if self.delegate is not None else self.step.agent
        if (getattr(owner, "description", "") or "").strip():
            about = f" Does: {owner.description.strip()}"
        tools = (
            f"tools: {', '.join(self.tools)}"
            if self.tools
            else "no tools: answers with its own knowledge in its role"
        )
        if self.delegate is not None:
            return f"{self.key}: agent '{self.name}' (via step '{label}'; {tools}).{about}"
        return f"{self.key}: worker '{label}' ({tools}).{about}"


@dataclass
class PlannedTask:
    instruction: str
    worker: Worker
    tool: str = ""
    depends_on: list = field(default_factory=list)  # earlier part numbers (1-based)
    title: str = ""

    @property
    def uses_previous(self) -> bool:
        return bool(self.depends_on)


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


def waves(tasks) -> list:
    """The 1-based "wave" of each task: 1 = starts at once, n = waits for a
    task of wave n-1. Tasks in the same wave run at the same time."""
    out: list = []
    for task in tasks:
        out.append(1 + max((out[d - 1] for d in task.depends_on), default=0))
    return out


def plan_tasks(
    *,
    llm,
    langfuse,
    message_text,
    history_text,
    workers,
    file_titles=(),
    tool_rules=None,
) -> list[PlannedTask]:
    """Split ``message_text`` into parts, each given to one worker.

    Unknown worker keys fall back to the worker that has the named tool (else
    the first worker); ``depends_on`` keeps only earlier part numbers. Only an
    ``LLMConfigurationError`` is raised; any other failure returns ``[]`` and
    the caller picks a single worker for the whole message.
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
        "rules": "\n".join(f"- {rule}" for rule in (tool_rules or {}).values())
        or "- Pick the tool whose name best fits the part.",
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
    except Exception:  # noqa: BLE001 - a failed plan falls back to one worker
        logger.warning("Supervisor plan failed; one worker answers the message", exc_info=True)
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
        tool = tool if tool in worker.tools else ""
        depends_on = sorted({d for d in item.depends_on if 1 <= d <= len(tasks)})
        if not depends_on and tasks and tool in EARLIER_OUTPUT_TOOLS:
            depends_on = [len(tasks)]
        tasks.append(
            PlannedTask(
                instruction=instruction,
                worker=worker,
                tool=tool,
                depends_on=depends_on,
                title=(item.title or "").strip(),
            )
        )
    return tasks
