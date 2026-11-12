"""The Agent's graph JSON (``Agent.graph``): what chat runs.

The builder canvas still edits Steps. Whenever a Step, a Step's tools or a
Tool changes, ``sync_agent_graph`` rewrites the Agent's ``graph`` column from
the steps reachable from START. Chat never reads Steps: it loads
``Agent.graph`` and compiles it (``chat.agent.graph.build_graph``).

Shape (version 1)::

    {
      "version": 1,
      "agent": "chat_agent",
      "nodes": [
        {"id": "__start__", "type": "start"},
        {"id": "main", "type": "model", "name": "Main step",
         "tools": ["create_chart", "document_search"]},
        {"id": "main__tools", "type": "tools", "for": "main"},
        {"id": "__end__", "type": "end"}
      ],
      "edges": [
        {"source": "__start__", "target": "main"},
        {"source": "main", "target": "main__tools", "condition": "tools"},
        {"source": "main", "target": "__end__", "condition": "next"},
        {"source": "main__tools", "target": "main"}
      ]
    }

A ``model`` node calls the agent's model with its ``tools`` bound; a
``tools`` node runs the tools of the model node it is ``for``. An edge with a
``condition`` is conditional: from a model node, ``"tools"`` is taken when the
reply asks for tools and ``"next"`` otherwise. ``general_assistant`` binds no
tool, so a step with only that tool is a single model node with no tools node.
"""
from __future__ import annotations

GRAPH_VERSION = 1
START = "__start__"
END = "__end__"
TOOLS_SUFFIX = "__tools"
ANSWER_DIRECTLY = "general_assistant"


def graph_from_chain(agent_slug: str, chain: list[dict]) -> dict:
    """Graph JSON for an ordered chain of ``{slug, name, tools}`` steps.

    ``{}`` for an empty chain (the agent has no runnable flow).
    """
    if not chain:
        return {}
    nodes = [{"id": START, "type": "start"}]
    edges = [{"source": START, "target": chain[0]["slug"]}]
    for i, step in enumerate(chain):
        slug = step["slug"]
        tools = sorted(set(step["tools"]) - {ANSWER_DIRECTLY})
        nxt = chain[i + 1]["slug"] if i + 1 < len(chain) else END
        node = {"id": slug, "type": "model", "name": step["name"] or slug}
        if tools:
            node["tools"] = tools
        nodes.append(node)
        if tools:
            tools_id = f"{slug}{TOOLS_SUFFIX}"
            nodes.append({"id": tools_id, "type": "tools", "for": slug})
            edges += [
                {"source": slug, "target": tools_id, "condition": "tools"},
                {"source": slug, "target": nxt, "condition": "next"},
                {"source": tools_id, "target": slug},
            ]
        else:
            edges.append({"source": slug, "target": nxt})
    nodes.append({"id": END, "type": "end"})
    return {"version": GRAPH_VERSION, "agent": agent_slug, "nodes": nodes, "edges": edges}


def walk_chain(steps: list[dict]) -> list[dict]:
    """The steps the flow runs, from plain step dicts.

    Starts at the lowest-``order`` (then oldest) START step and follows
    ``next_step``; stops at an END step, a missing hop, a cycle, or after
    ``2 * len(steps) + 1`` hops.

    Each dict: ``pk, slug, name, tools, connected_from_start,
    connected_to_end, next_step_id, order, created_at``.
    """
    by_pk = {s["pk"]: s for s in steps}
    entries = sorted(
        (s for s in steps if s["connected_from_start"]),
        key=lambda s: (s["order"], s["created_at"]),
    )
    if not entries:
        return []
    cap = 2 * len(steps) + 1
    chain, seen, current = [], set(), entries[0]
    while current is not None and len(chain) < cap:
        if current["pk"] in seen:
            break
        chain.append(current)
        seen.add(current["pk"])
        if current["connected_to_end"]:
            break
        current = by_pk.get(current["next_step_id"])
    return chain


def step_dicts(step_qs) -> list[dict]:
    """Plain dicts for live steps (works with historical migration models too);
    only enabled, live tools count."""
    return [
        {
            "pk": s.pk,
            "slug": s.slug,
            "name": s.name,
            "tools": list(
                s.tools.filter(is_enabled=True, is_deleted=False).values_list("slug", flat=True)
            ),
            "connected_from_start": s.connected_from_start,
            "connected_to_end": s.connected_to_end,
            "next_step_id": s.next_step_id,
            "order": s.order,
            "created_at": s.created_at,
        }
        for s in step_qs
    ]


def build_agent_graph(agent) -> dict:
    from agents.models import Step

    steps = step_dicts(Step.objects.filter(agent=agent).prefetch_related("tools"))
    return graph_from_chain(agent.slug, walk_chain(steps))


def sync_agent_graph(agent) -> dict:
    """Rewrite ``agent.graph`` from its steps (no Agent save signals run)."""
    from agents.models import Agent

    graph = build_agent_graph(agent)
    if agent.graph != graph:
        Agent.all_objects.filter(pk=agent.pk).update(graph=graph)
        agent.graph = graph
    return graph
