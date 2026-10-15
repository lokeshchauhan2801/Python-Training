"""The ``company.Company`` model (tenant root).

``Company`` composes ``BaseModel`` (4 audit fields) + ``SoftDeleteModel`` and
declares its own ULID ``id`` PK. As the tenant root it has an ``owner`` FK but
NO ``company`` FK (it IS the company), so it is the one domain model without a
tenant-scoping company field.
"""

from django.conf import settings
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class Company(BaseModel, SoftDeleteModel):
    id = ULIDField()
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
