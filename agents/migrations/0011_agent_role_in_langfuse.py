"""Tool docs replace tool prompts; agent roles move to Langfuse.

1. Empty ``Tool.description`` gets the built-in tool's docs (the text its
   Langfuse ``tool_<slug>`` prompt held), frozen here.
2. Every ``Agent.description`` becomes the agent's Langfuse role prompt
   ``agents/<company slug>/<agent slug>``, worded as ``_persona`` used it.
   Langfuse must be configured when any agent has a description; the column
   is dropped only after every prompt is saved in Langfuse.
3. ``Agent.description`` is removed.
"""

from django.db import migrations

TOOL_DOCS = {
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
    "report_writer": (
        "You are a report writer. Turn the earlier results into a short, clear "
        "report with three parts: Summary (2-3 sentences), Key findings (bullet"
        " points with the important facts and figures), and Next steps (only if"
        " they follow from the results). Use only facts from the earlier "
        "results and the conversation; never invent figures."
    ),
    "action_items": (
        "You extract action items. List every concrete task found in the "
        "earlier results as numbered items: what must be done, who owns it and "
        "the deadline when the text says so (write 'owner not stated' / 'no "
        "deadline' otherwise). If there are no tasks, say so in one sentence."
    ),
    "email_drafter": (
        "You draft emails. Write a ready-to-send email that shares the earlier "
        "results with the recipient the user names (or a neutral 'Hi team' when"
        " none is named). Give a Subject line, then a short body: greeting, the"
        " key points from the earlier results, and a polite closing. Keep facts"
        " exactly as in the earlier results."
    ),
    "translator": (
        "You are a translator. Translate the earlier results into the language "
        "the user asks for (English if none is named). Keep names, numbers, "
        "file names and the structure (lists, headings) unchanged. Reply with "
        "the translation only."
    ),
    "chart_builder": (
        "Chart the numbers found in the earlier steps' output."
    ),
}


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    Tool = apps.get_model("agents", "Tool")

    for slug, docs in TOOL_DOCS.items():
        Tool.objects.filter(slug=slug, description="").update(description=docs)

    agents = [
        a
        for a in Agent.objects.select_related("company")
        if (a.description or "").strip()
    ]
    if not agents:
        return

    from basics.langfuse import get_langfuse_client

    client = get_langfuse_client()
    if not client.configured:
        raise RuntimeError(
            "Agent descriptions must move to Langfuse but LANGFUSE_* keys are "
            "missing. Set them (source ~/.bash_profile) and run migrate again."
        )
    for agent in agents:
        company = agent.company.slug if agent.company_id else "global"
        name = f"agents/{company}/{agent.slug}"
        text = (
            f"You are the '{agent.name or agent.slug}' agent. "
            f"Your job: {agent.description.strip()}"
        )
        # A new version, not ensure_prompt: a running server's startup sync
        # may already have made a starter prompt under this name.
        if not client.create_prompt(name, text):
            raise RuntimeError(f"Could not create Langfuse prompt {name}; nothing was dropped.")
    client.flush()


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0010_remove_tool_type"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name="agent", name="description"),
    ]
