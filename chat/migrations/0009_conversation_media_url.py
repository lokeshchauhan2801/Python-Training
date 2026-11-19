"""Record a turn's attachment URL in ``Conversation.media_url``.

Each session file entry gets its ``media_url``; each turn's
``context["attachments"]`` chips become the first attached file's media URL,
and the key is dropped from the turn's context.
"""
from django.core.files.storage import default_storage
from django.db import migrations, models


def _media_url(key):
    url = default_storage.url(key)
    if not url.startswith(("http://", "https://", "/")):
        url = "/" + url
    return url


def _entries(context):
    context = context if isinstance(context, dict) else {}
    return [
        entry
        for name in ("unprocessed_docs", "processed_docs")
        for entry in (context.get(name) or [])
        if isinstance(entry, dict)
    ]


def forwards(apps, schema_editor):
    Session = apps.get_model("chat", "ConversationSession")
    Conversation = apps.get_model("chat", "Conversation")

    urls_by_session = {}
    for session in Session._base_manager.all().iterator():
        context = session.context if isinstance(session.context, dict) else {}
        by_id = {}
        changed = False
        for entry in _entries(context):
            key = entry.get("storage_key")
            if not key:
                continue
            if not entry.get("media_url"):
                entry["media_url"] = _media_url(key)
                changed = True
            by_id[str(entry.get("id"))] = entry["media_url"]
        if changed:
            Session._base_manager.filter(pk=session.pk).update(context=context)
        urls_by_session[session.pk] = by_id

    for turn in Conversation._base_manager.filter(
        context__has_key="attachments"
    ).iterator():
        context = dict(turn.context or {})
        chips = context.pop("attachments", None) or []
        by_id = urls_by_session.get(turn.session_id, {})
        urls = [
            by_id[str(chip.get("id"))]
            for chip in chips
            if isinstance(chip, dict) and str(chip.get("id")) in by_id
        ]
        Conversation._base_manager.filter(pk=turn.pk).update(
            context=context, media_url=urls[0] if urls else ""
        )


def backwards(apps, schema_editor):
    Session = apps.get_model("chat", "ConversationSession")
    Conversation = apps.get_model("chat", "Conversation")

    chips_by_session = {}
    for session in Session._base_manager.all().iterator():
        chips_by_session[session.pk] = {
            entry.get("media_url"): {
                "id": str(entry.get("id") or ""),
                "title": str(entry.get("title") or ""),
            }
            for entry in _entries(session.context)
            if entry.get("media_url")
        }

    for turn in Conversation._base_manager.exclude(media_url="").iterator():
        chip = chips_by_session.get(turn.session_id, {}).get(turn.media_url)
        if chip:
            context = dict(turn.context or {})
            context["attachments"] = [chip]
            Conversation._base_manager.filter(pk=turn.pk).update(context=context)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0008_langgraph_checkpoint_tables"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="media_url",
            field=models.URLField(blank=True, max_length=1000),
        ),
        migrations.RunPython(forwards, backwards),
    ]
