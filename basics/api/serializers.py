"""Reusable DRF serializer bases."""


class AuditReadOnlySerializerMixin:
    """Force server-controlled fields to read-only.

    Only the fields actually declared on the serializer are affected (the
    intersection with ``self.fields``), so the same mixin is safe across models
    that each lack some of these fields. ``provider`` is intentionally NOT in the
    list: it is not a model field (it lives inside ``Conversation.metadata``).
    """

    SERVER_CONTROLLED = [
        "user",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "is_deleted",
        "deleted_at",
        "model_name",
        "response",
        "summary",
        "status",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.SERVER_CONTROLLED:
            if name in self.fields:
                self.fields[name].read_only = True
