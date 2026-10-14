"""User managers composed from Django's ``UserManager`` and soft-delete.

``UserManager`` keeps the full Django user-management API (``create_user`` /
``create_superuser`` / ``normalize_email``) while hiding soft-deleted users from
normal queries. ``AllUsersManager`` keeps the same API but returns every row
(used by admin restore / recovery). ``User`` deliberately does NOT reuse
``basics.AllObjectsManager`` because that lacks the Django user API.
"""

from django.contrib.auth.models import UserManager as DjangoUserManager

from basics.managers import SoftDeleteQuerySet


class UserQuerySet(SoftDeleteQuerySet):
    pass


class UserManager(DjangoUserManager.from_queryset(UserQuerySet)):
    """Default manager: full UserManager API + hides soft-deleted users."""

    use_in_migrations = True

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

    def create_user(self, username=None, email=None, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        if not username:
            username = email
        return super().create_user(
            username=username, email=email, password=password, **extra_fields
        )

    def create_superuser(
        self, username=None, email=None, password=None, **extra_fields
    ):
        if not email:
            raise ValueError("Superusers must have an email address.")
        email = self.normalize_email(email)
        if not username:
            username = email
        return super().create_superuser(
            username=username, email=email, password=password, **extra_fields
        )


class AllUsersManager(DjangoUserManager.from_queryset(UserQuerySet)):
    """Recovery/restore manager: full UserManager API, NO is_deleted filter."""

    use_in_migrations = False
    # NO get_queryset override -> returns every row, including soft-deleted.
