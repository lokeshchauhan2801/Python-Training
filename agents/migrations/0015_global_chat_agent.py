"""Keep one global chat agent.

1. The ``chat_agent`` role prompt in Langfuse gets its full text when it still
   holds the starter line.
2. Every agent is deleted except one global ``chat_agent`` (no company, so
   every company can use it), which is created with the six built-in tools as
   global tool rows on one START -> main -> END step. Sessions that selected a
   deleted agent go back to Normal RAG (``selected_agent`` is SET_NULL).
"""

from django.db import migrations

CHAT_AGENT_ROLE = (
    "You are Chat agent, the assistant every company uses in this chat app.\n\n"
    "What you can do:\n"
    "- Answer general questions clearly, and build pie, bar or line charts when asked.\n"
    "- Answer questions from the company's documents and knowledge base, and from "
    "files attached in this chat, using only what those documents say.\n"
    "- Give current or external information when the user asks for web or online "
    "sources, saying when it may be out of date.\n"
    "- Summarise or recap this conversation.\n"
    "- Add attached files to the company knowledge base, and report the processing "
    "status of documents.\n\n"
    "How you work:\n"
    "- Reply in the language the user writes in.\n"
    "- When a question is about company data, answer from the documents and say "
    "plainly when they do not contain the answer instead of guessing.\n"
    "- Keep answers short and direct; offer more detail when it would help.\n"
    "- If a request is unclear, ask one short question before answering."
)

CHAT_AGENT_STARTER = "You are the 'Chat agent' agent."

TOOLS = [
    ("general_assistant", "General assistant"),
    ("document_search", "Document search"),
    ("chat_summarise", "Chat summary"),
    ("web_search", "Web search"),
    ("add_to_knowledge_base", "Add to knowledge base"),
    ("document_processing_status", "Document processing status"),
]


def push_prompts():
    from basics.langfuse import get_langfuse_client

    client = get_langfuse_client()
    current = client.get_prompt("chat_agent", fallback="").strip()
    if not current or current == CHAT_AGENT_STARTER:
        client.create_prompt("chat_agent", CHAT_AGENT_ROLE)
    client.flush()


def forwards(apps, schema_editor):
    from agents.services.tool_docs import DEFAULT_TOOL_DOCS

    Agent = apps.get_model("agents", "Agent")
    Step = apps.get_model("agents", "Step")
    Tool = apps.get_model("agents", "Tool")

    push_prompts()

    keep = Agent._base_manager.filter(
        company__isnull=True, slug="chat_agent", is_deleted=False
    ).first()
    Agent._base_manager.exclude(pk=getattr(keep, "pk", None)).delete()
    if keep is not None:
        return

    tools = []
    for slug, name in TOOLS:
        tool = Tool._base_manager.filter(
            company__isnull=True, slug=slug, is_deleted=False
        ).first() or Tool._base_manager.create(
            company=None,
            slug=slug,
            name=name,
            description=DEFAULT_TOOL_DOCS.get(slug, ""),
            is_enabled=True,
        )
        tools.append(tool)

    agent = Agent._base_manager.create(company=None, slug="chat_agent", name="Chat agent")
    step = Step._base_manager.create(
        agent=agent,
        company=None,
        slug="main",
        name="Main step",
        connected_from_start=True,
        connected_to_end=True,
        order=0,
    )
    step.tools.set(tools)


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0014_step_next_or_end"),
        ("chat", "0007_conversationsession_selected_agent"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
