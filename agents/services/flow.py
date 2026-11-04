"""The single authoritative reachable-Step walker.

``walk_reachable_steps(agent)`` is the ONE source of truth for which Steps an
Agent's pipeline will execute. Both the LangGraph runtime
(``chat.services.langgraph_service``) and the capability preflight
(``chat.services.agent_capabilities``) call it directly, so a reachability
change can never let a policy allow an action that execution would not traverse.

The walk is dependency-light and read-only:

* It imports ``Step`` locally to avoid an app-import cycle.
* It uses the soft-delete-aware default manager (``Step.objects``), never an FK
  descriptor, so a soft-deleted / missing / foreign entry or hop terminates the
  chain.
* It is cycle- and cap-guarded, so any malformed graph still terminates.
* It performs no writes.
"""

from __future__ import annotations


def walk_reachable_steps(agent) -> list:
    """Return the ordered, unique list of Steps the ``agent`` pipeline runs.

    ``[]`` when ``agent`` is ``None`` or has no live entry Step. Otherwise the
    chain starts at the lowest-``order`` (then oldest ``created_at``) Step with
    ``connected_from_start=True`` and follows ``next_step`` within the same
    Agent. A ``connected_to_end`` Step is appended and terminates the chain even
    if it still has a next pointer. The walk otherwise stops before a duplicate
    primary key (cycle), a missing / foreign / soft-deleted next hop, or after
    ``2 * <live step count> + 1`` appended hops.
    """
    if agent is None:
        return []

    from agents.models import Step

    entry = (
        Step.objects.filter(agent=agent, connected_from_start=True)
        .order_by("order", "created_at")
        .first()
    )
    if entry is None:
        return []

    cap = 2 * Step.objects.filter(agent=agent).count() + 1
    chain: list = []
    seen: set = set()
    current = entry
    while current is not None and len(chain) < cap:
        if current.pk in seen:
            break
        chain.append(current)
        seen.add(current.pk)
        if current.connected_to_end:
            break
        next_id = current.next_step_id
        if next_id is None:
            break
        current = Step.objects.filter(pk=next_id, agent=agent).first()
    return chain


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
    """The reachable Steps that actually run tools, delegation expanded.

    A delegating step is replaced by its delegate Agent's own tool steps
    (recursively, cycle- and ``MAX_DELEGATION_DEPTH``-guarded). Read-only; the
    capability preflight uses it so a tool on a sub-agent counts.
    """
    seen = set(_seen or ()) | {getattr(agent, "pk", None)}
    steps: list = []
    for step in walk_reachable_steps(agent):
        sub = delegate_of(step)
        if sub is None:
            steps.append(step)
        elif sub.pk not in seen and _depth < MAX_DELEGATION_DEPTH:
            steps.extend(walk_tool_steps(sub, _depth=_depth + 1, _seen=seen))
    return steps


def delegation_reaches(agent, target, *, _depth=0, _seen=None) -> bool:
    """True when ``agent``'s steps delegate (directly or not) to ``target``.

    Looks at EVERY live step (reachable or not) so wiring a step later can
    never complete a cycle the validation missed.
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
