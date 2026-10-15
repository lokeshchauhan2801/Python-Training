"""The ``accounts.User`` model.

``User`` composes the project-wide ``BaseModel`` (the 4 audit fields +
actor-aware save) and ``SoftDeleteModel`` (is_deleted/deleted_at + soft delete)
alongside ``AbstractUser`` (Django auth/password/permission machinery), and
declares its own ULID ``id`` PK. The 4 audit fields come from ``BaseModel`` —
``User`` re-declares NONE of them. ``AbstractUser`` still supplies
``date_joined``/``last_login``/``is_staff``/``is_superuser``/groups/permissions.
``User`` has NO company FK.
"""

from django.db import models

from django.contrib.auth.models import AbstractUser

from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField

from .managers import AllUsersManager, UserManager


class User(BaseModel, SoftDeleteModel, AbstractUser):
    id = ULIDField()
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    objects = UserManager()
    all_objects = AllUsersManager()

    class Meta:
        db_table = "accounts_user"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]

    def __str__(self) -> str:
        return self.email
