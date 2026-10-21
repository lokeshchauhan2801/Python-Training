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
