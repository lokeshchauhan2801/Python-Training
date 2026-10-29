"""Shared helpers for the chat file and document-control tests.

Not named ``test*.py`` so Django's runner does not collect it as a test module.
"""
from __future__ import annotations

from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile

from documents.models import Document


def fake_enqueue(document_id, backend=None):
    """Stand-in for ``enqueue_document_processing`` that never contacts Temporal.

    Flips the row to QUEUED like the real function, without the executor
    callback.
    """
    task_id = str(uuid4())
    Document.objects.filter(pk=document_id).update(
        status=Document.STATUS_QUEUED,
        processing_stage="queued",
        processing_progress=0,
        processing_backend="temporal",
        processing_task_id=task_id,
        processing_error="",
    )
    return task_id, "temporal"


def make_upload(name, content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    return SimpleUploadedFile(name, content)


def attach_entry(session, name, content, actor):
    from chat.services import session_files

    return session_files.store_upload(session, make_upload(name, content), actor)
