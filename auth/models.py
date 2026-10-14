"""The ``accounts.User`` model.

Subclasses ``AbstractUser`` (so the Django auth/password/permission machinery is
preserved) and re-declares the audit + soft-delete columns EXPLICITLY (MEDIUM
FIX 2): ``AbstractUser`` provides ``date_joined`` but NOT ``created_at``, and
``User`` cannot inherit the abstract ``AuditModel`` (it already inherits
``AbstractUser``), so the audit fields are declared here by hand with the
``accounts_user_created`` / ``accounts_user_updated`` related names. ``User`` has
NO company FK.
"""

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from basics.models import resolve_actor

from .managers import AllUsersManager, UserManager


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    # MEDIUM FIX 2: explicit re-declared audit/soft-delete fields. User gets its
    # OWN created_at (AbstractUser has date_joined, not created_at).
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="accounts_user_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="accounts_user_updated",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()
    all_objects = AllUsersManager()

    class Meta:
        db_table = "accounts_user"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return self.email

    def save(self, *args, actor=None, **kwargs):
        """Actor-aware save mirroring ``AuditModel`` (tolerates no actor)."""
        resolved = resolve_actor(actor)
        if resolved is not None:
            if self._state.adding and self.created_by_id is None:
                self.created_by = resolved
            self.updated_by = resolved
        super().save(*args, **kwargs)

    def delete(self, *args, actor=None, **kwargs):
        """Soft delete this user (partial save of the soft-delete columns)."""
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.updated_by = resolve_actor(actor)
        self.save(
            update_fields=["is_deleted", "deleted_at", "updated_by", "updated_at"]
        )

    def hard_delete(self, *args, **kwargs):
        """Permanently remove this user."""
        return super().delete(*args, **kwargs)

    def restore(self, *args, actor=None):
        self.is_deleted = False
        self.deleted_at = None
        self.updated_by = resolve_actor(actor)
        self.save(
            update_fields=["is_deleted", "deleted_at", "updated_by", "updated_at"]
        )
