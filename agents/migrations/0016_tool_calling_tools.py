"""Built-in tools for official LangGraph tool calling.

Each Step of an Agent's flow becomes a LangGraph model node with the step's
tools bound (``chat/agent/graph.py``), so:

1. Built-in tool rows whose description is still the old handler prompt get
   the tool-calling description (an edited description is kept).
2. Global ``create_chart`` and ``chart_from_file`` tool rows are created.
3. Steps with ``general_assistant`` (which used to draw charts) get
   ``create_chart``; the global ``chat_agent``'s steps with
   ``document_search`` get ``chart_from_file``.

Steps and their layout are not changed otherwise.
"""

from django.db import migrations

OLD_TOOL_DOCS = {
    "general_assistant": (
        "You are a helpful general assistant. Answer the user's question "
        "clearly and concisely using your general knowledge. If the user asks "
        "for a chart, graph, or plot (pie, bar, or line), build it from your "
        "best real numeric values and return it in the structured chart field "
        "only — never put chart JSON in the response text. Add a short "
        "\"(general knowledge)\" marker in the chart title and a one-line caveat "
        "that the figures are approximate and may be out of date. If you "
        "genuinely have no real values, set the chart to null and say so."
    ),
    "document_search": (
        "Answer strictly from the retrieved document passages below. Cite the "
        "sources you use and do not invent facts. If the user asks for a chart,"
        " graph, or plot, build it only from values present in the retrieved "
        "passages and return it in the structured chart field only — never put "
        "chart JSON in the response text. If the passages do not contain the "
        "requested figures, set the chart to null and explain that the document"
        " does not contain them."
    ),
    "chat_summarise": (
        "You are a helpful assistant. Summarize the following conversation "
        "concisely, capturing the key questions and conclusions. Format the "
        "visible response as plain text with normal sentences, dot bullets, or "
        "numbered lists. Do not use Markdown emphasis or asterisk bullets."
    ),
    "web_search": (
        "You are a web-search assistant. Summarise the most relevant findings "
        "for the user's query."
    ),
    "add_to_knowledge_base": (
        "Add an attached file to the knowledge base (background processing)."
    ),
    "document_processing_status": (
        "Report the status of documents being processed."
    ),
}

NEW_TOOLS = [
    ("create_chart", "Create chart"),
    ("chart_from_file", "Chart from file"),
]


def _tool(Tool, company_id, slug, name, docs):
    return Tool._base_manager.filter(
        company_id=company_id, slug=slug, is_deleted=False
    ).first() or Tool._base_manager.create(
        company_id=company_id,
        slug=slug,
        name=name,
        description=docs.get(slug, ""),
        is_enabled=True,
    )


def forwards(apps, schema_editor):
    from agents.services.tool_docs import DEFAULT_TOOL_DOCS

    Step = apps.get_model("agents", "Step")
    Tool = apps.get_model("agents", "Tool")

    for slug, old in OLD_TOOL_DOCS.items():
        new = DEFAULT_TOOL_DOCS.get(slug)
        if new:
            Tool._base_manager.filter(slug=slug, description=old).update(description=new)

    for slug, name in NEW_TOOLS:
        _tool(Tool, None, slug, name, DEFAULT_TOOL_DOCS)

    for step in Step._base_manager.filter(is_deleted=False).select_related("agent"):
        slugs = set(step.tools.filter(is_deleted=False).values_list("slug", flat=True))
        company_id = step.agent.company_id
        if "general_assistant" in slugs and "create_chart" not in slugs:
            step.tools.add(_tool(Tool, company_id, "create_chart", "Create chart", DEFAULT_TOOL_DOCS))
        if (
            company_id is None
            and step.agent.slug == "chat_agent"
            and "document_search" in slugs
            and "chart_from_file" not in slugs
        ):
            step.tools.add(_tool(Tool, None, "chart_from_file", "Chart from file", DEFAULT_TOOL_DOCS))


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0015_global_chat_agent"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
