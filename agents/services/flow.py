"""Which Steps (workers) an Agent has, and how delegation between Agents nests.

An Agent is a supervisor over its Steps: every live Step is a worker the
supervisor can give part of a message to (there is no fixed order to walk).
``agent_workers(agent)`` is the ONE source of truth for that list. Both the
LangGraph runtime (``chat.services.langgraph_service``) and the capability
preflight (``chat.services.agent_capabilities``) use it, so policy can never
allow an action the runtime could not route to.

Everything here is read-only and imports models locally (no app-import cycle).
It uses the soft-delete-aware default managers, never FK descriptors, so a
soft-deleted worker or delegate simply drops out.
"""

from __future__ import annotations


def agent_workers(agent) -> list:
    """The live Steps of ``agent`` (its workers), ``order`` then oldest first.

    ``[]`` when ``agent`` is ``None`` or has no live Step.
    """
    if agent is None:
        return []

    from agents.models import Step

    return list(Step.objects.filter(agent=agent).order_by("order", "created_at"))


# How many agents deep a delegation chain may go (agent -> sub-agent -> ...).
MAX_DELEGATION_DEPTH = 3


def delegate_of(step):
    """The live Agent ``step`` hands its work to, or ``None``.

    Uses the soft-delete-aware manager (never the FK descriptor), so a deleted
    or foreign-company delegate makes the step use its own tools again.
    """
    if step is None or not step.delegate_agent_id:
        return None
    from agents.models import Agent

    return Agent.objects.filter(
        pk=step.delegate_agent_id, company_id=step.company_id
    ).exclude(pk=step.agent_id).first()


def walk_tool_steps(agent, *, _depth=0, _seen=None) -> list:
    """The workers that actually run tools, delegation expanded.

    A delegating step is replaced by its delegate Agent's own tool steps
    (recursively, cycle- and ``MAX_DELEGATION_DEPTH``-guarded). Read-only; the
    capability preflight uses it so a tool on a sub-agent counts.
    """
    seen = set(_seen or ()) | {getattr(agent, "pk", None)}
    steps: list = []
    for step in agent_workers(agent):
        sub = delegate_of(step)
        if sub is None:
            steps.append(step)
        elif sub.pk not in seen and _depth < MAX_DELEGATION_DEPTH:
            steps.extend(walk_tool_steps(sub, _depth=_depth + 1, _seen=seen))
    return steps


def delegation_reaches(agent, target, *, _depth=0, _seen=None) -> bool:
    """True when ``agent``'s steps delegate (directly or not) to ``target``.

    Looks at EVERY live step, so no later change can complete a cycle the
    validation missed.
    """
    if agent is None or target is None:
        return False
    if agent.pk == target.pk:
        return True
    seen = set(_seen or ()) | {agent.pk}
    if _depth > 2 * MAX_DELEGATION_DEPTH:
        return False
    from agents.models import Agent, Step

    sub_ids = (
        Step.objects.filter(agent=agent, delegate_agent__isnull=False)
        .values_list("delegate_agent_id", flat=True)
    )
    for sub in Agent.objects.filter(pk__in=list(sub_ids)):
        if sub.pk in seen:
            continue
        if delegation_reaches(sub, target, _depth=_depth + 1, _seen=seen):
            return True
    return False
