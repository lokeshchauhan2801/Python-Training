"""Composable abstract base models.

Three independent, composable pieces (ONE each, no per-module variants):

* ``BaseModel`` — the single audit base inherited by EVERY model (including
  ``accounts.User``). It holds EXACTLY FOUR fields: ``created_at``,
  ``updated_at``, ``created_by``, ``updated_by``, plus the actor-aware ``save``
  that stamps the audit actors. Nothing else.
* The ULID primary key is a SEPARATE reusable field (``basics.ulid.ULIDField``);
  each model declares its own ``id = ULIDField()``. The PK is NOT on ``BaseModel``.
* ``SoftDeleteModel`` — a SEPARATE abstract mixin holding ``is_deleted`` /
  ``deleted_at`` and the actor-aware soft ``delete``/``restore``/``hard_delete``.
  Applied only to the models that need soft-delete, together with the
  soft-delete managers. Soft-delete is NOT on ``BaseModel``.

A fully-featured model composes ``BaseModel`` + ``SoftDeleteModel`` + an
``id = ULIDField()`` PK (and, for non-auth models, a required ``company`` FK).
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


def resolve_actor(explicit):
    """Resolve the audit actor.

    1. An explicit, saved user wins.
    2. Else the current user from the request contextvar.
    3. Else ``None``.
    """
    if explicit is not None and getattr(explicit, "pk", None) is not None:
        return explicit
    from .context import get_current_user

    current = get_current_user()
    if current is not None and getattr(current, "pk", None) is not None:
        return current
    return None


class BaseModel(models.Model):
    """The single audit base — EXACTLY four audit fields and nothing else."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="%(app_label)s_%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="%(app_label)s_%(class)s_updated",
    )

    class Meta:
        abstract = True

    def save(self, *args, actor=None, **kwargs):
        resolved = resolve_actor(actor)
        if resolved is not None:
            if self._state.adding and self.created_by_id is None:
                self.created_by = resolved
            self.updated_by = resolved
        super().save(*args, **kwargs)


class SoftDeleteModel(models.Model):
    """Separate soft-delete mixin: ``is_deleted`` / ``deleted_at`` + methods."""

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def delete(self, *args, actor=None, **kwargs):
        """Soft delete this instance (partial save of the soft-delete columns)."""
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.updated_by = resolve_actor(actor)
        self.save(
            update_fields=["is_deleted", "deleted_at", "updated_by", "updated_at"]
        )

    def hard_delete(self, *args, **kwargs):
        """Permanently remove this instance."""
        return super().delete(*args, **kwargs)

    def restore(self, *args, actor=None):
        self.is_deleted = False
        self.deleted_at = None
        self.updated_by = resolve_actor(actor)
        self.save(
            update_fields=["is_deleted", "deleted_at", "updated_by", "updated_at"]
        )
