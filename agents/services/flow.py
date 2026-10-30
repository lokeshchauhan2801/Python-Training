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
