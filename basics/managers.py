"""Soft-delete queryset and managers.

The queryset owns the soft-delete semantics so every model behaves identically.
``SoftDeleteManager`` (the default ``objects``) hides deleted rows;
``AllObjectsManager`` (``all_objects``) sees everything, for recovery/restore.
"""

from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    def delete(self, actor=None):
        """Soft delete: bulk-mark rows as deleted. Returns the affected count."""
        from .models import resolve_actor

        return self.update(
            is_deleted=True,
            deleted_at=timezone.now(),
            updated_at=timezone.now(),
            updated_by=resolve_actor(actor),
        )

    def hard_delete(self):
        """Permanently delete the rows via the real SQL DELETE."""
        return super().delete()

    def alive(self):
        return self.filter(is_deleted=False)

    def deleted(self):
        return self.filter(is_deleted=True)

    def restore(self, actor=None):
        """Un-delete rows in bulk. Returns the affected count."""
        from .models import resolve_actor

        return self.update(
            is_deleted=False,
            deleted_at=None,
            updated_at=timezone.now(),
            updated_by=resolve_actor(actor),
        )


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager — hides soft-deleted rows from normal queries."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Recovery manager — returns every row, including soft-deleted ones."""
