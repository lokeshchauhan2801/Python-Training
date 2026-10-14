"""The ``company.Company`` model (tenant root).

``Company`` inherits the company-free ``AuditModel`` (UUID pk + audit +
soft-delete) rather than ``BaseModel``, because it IS the tenant root and has no
self-referential company FK.
"""

from django.conf import settings
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import AuditModel


class Company(AuditModel):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="companies",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=100, unique=True, db_index=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "company"
        ordering = ["name"]
        verbose_name = "Company"
        verbose_name_plural = "Companies"

    def __str__(self) -> str:
        return self.name
