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
