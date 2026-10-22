from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class ScrapeJob(BaseModel, SoftDeleteModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    url = models.URLField(max_length=2048)
    title = models.CharField(max_length=512, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status,
        default=Status.PENDING,
        db_index=True,
    )
    content = models.TextField(blank=True)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "scrapper_scrape_job"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title or self.url
