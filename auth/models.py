from django.db import models

from django.contrib.auth.models import AbstractUser

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField

from .managers import AllUsersManager, UserManager


class User(BaseModel, SoftDeleteModel, AbstractUser):
    id = ULIDField()
    email = models.EmailField(unique=True)
    accessible_companies = models.ManyToManyField(
        "company.Company",
        through="CompanyMembership",
        through_fields=("user", "company"),
        related_name="member_users",
        blank=True,
        help_text="Companies this user can access (each with a role).",
    )

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


class OIDCIdentity(models.Model):
    """Links a Django user to exactly one Keycloak account by its ``sub``.

    Keycloak logins are matched ONLY through this table, never by email, so a
    Keycloak account cannot take over a local user that merely shares an email.
    Rows are created on first Keycloak login (new users) or by the
    ``link_keycloak_identities`` command (existing users).
    """

    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="oidc_identity",
    )
    sub = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_oidc_identity"
        verbose_name = "Keycloak identity"
        verbose_name_plural = "Keycloak identities"

    def __str__(self) -> str:
        return f"{self.user_id} <- {self.sub}"


class CompanyMembership(BaseModel, SoftDeleteModel):
    """A user's membership in a company, with a role.

    Lets one user belong to MULTIPLE companies, each with its own role. Access
    (``company.active.allowed_companies_for_user``) counts a user as able to use
    a company when they own it OR have a membership here.
    """

    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_VIEWER = "viewer"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Admin"),
        (ROLE_MEMBER, "Member"),
        (ROLE_VIEWER, "Viewer"),
    ]

    id = ULIDField()
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="company_memberships",
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(
        max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "accounts_company_membership"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "company"],
                name="accounts_user_company_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.company_id} ({self.role})"
