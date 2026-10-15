"""ULID utility.

Wraps the maintained ``python-ulid`` package. A ULID is a 26-character Crockford
base32 string that is lexicographically sortable by creation time.

``ULIDField`` is the single canonical primary-key field used by the one
``BaseModel`` (and therefore every model in the project): a 26-char ``CharField``
whose default is a fresh ULID. The generator is passed by reference, never called
at import time, so the field is import-safe and migration-serializable by dotted
path (``basics.ulid.ULIDField`` / ``basics.ulid.new_ulid``).
"""

from django.db import models
from ulid import ULID


def new_ulid() -> str:
    """Return a new 26-character ULID string."""
    return str(ULID())


class ULIDField(models.CharField):
    """A 26-char ULID string primary key.

    Fixes ``max_length``, ``primary_key``, ``editable`` and ``default`` so every
    model shares one PK definition. ``deconstruct`` drops those fixed kwargs so
    regenerated migrations only reference ``basics.ulid.ULIDField``.
    """

    def __init__(self, *args, **kwargs):
        kwargs["max_length"] = 26
        kwargs["primary_key"] = True
        kwargs["editable"] = False
        kwargs["default"] = new_ulid
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        for key in ("max_length", "primary_key", "editable", "default"):
            kwargs.pop(key, None)
        return name, path, args, kwargs
