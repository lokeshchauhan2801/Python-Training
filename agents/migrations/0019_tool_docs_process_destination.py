"""Refresh the stored docs of three built-in tools.

* ``add_to_knowledge_base`` now processes a file only in the chat unless the
  user asks for the company knowledge base (and asks when unclear).
* ``document_processing_status`` reads the status from Temporal.
* ``web_search`` no longer tells the model to stop at "no web access".

Only rows still holding the previous default text are updated; a description
someone edited is left alone.
"""

from django.db import migrations

OLD_DOCS = {
    "add_to_knowledge_base": (
        "Process this chat's files into the company knowledge base (background "
        "processing), or stop, resume, cancel or list that processing. Use it "
        "when the user asks to add, save, upload, index or process a file."
    ),
    "document_processing_status": (
        "Report the processing status of this chat's files and documents, or "
        "stop, resume, cancel or list their processing. Use it when the user "
        "asks what is processing, whether a file is done, or for a status."
    ),
    "web_search": (
        "Use when the user asks for web, online, current or external "
        "information. This app has no live web access: the tool says so, and "
        "you then answer from your own knowledge with a short note that it may "
        "be out of date."
    ),
}


def refresh(apps, schema_editor):
    from agents.services.tool_docs import DEFAULT_TOOL_DOCS

    Tool = apps.get_model("agents", "Tool")
    for slug, old in OLD_DOCS.items():
        Tool._base_manager.filter(slug=slug, description=old).update(
            description=DEFAULT_TOOL_DOCS[slug]
        )


def restore(apps, schema_editor):
    from agents.services.tool_docs import DEFAULT_TOOL_DOCS

    Tool = apps.get_model("agents", "Tool")
    for slug, old in OLD_DOCS.items():
        Tool._base_manager.filter(
            slug=slug, description=DEFAULT_TOOL_DOCS[slug]
        ).update(description=old)


class Migration(migrations.Migration):
    dependencies = [("agents", "0018_agent_layout_single_chain")]

    operations = [migrations.RunPython(refresh, restore)]
