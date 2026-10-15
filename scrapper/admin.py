"""Admin registration for ``scrapper.ScrapeJob``.

``ScrapeJobAdmin`` uses the reusable ``BaseModelAdmin`` (soft-delete visibility,
owner scoping, audit stamping) and adds a CSRF-protected confirmation flow for
running a scrape. The custom ``<str:pk>/run/`` URL is registered BEFORE
``super().get_urls()`` and wrapped in ``admin_view`` (enforces admin login +
permission). GET renders a confirmation page carrying ``{% csrf_token %}``; the
POST is CSRF-validated by Django's middleware before the job is resolved from the
permission-scoped queryset and run via ``ScrapeService``.
"""

from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from basics.admin import BaseModelAdmin

from .models import ScrapeJob
from .services import ScrapeService


@admin.register(ScrapeJob)
class ScrapeJobAdmin(BaseModelAdmin):
    list_display = ("title", "url", "status", "company", "is_deleted", "created_at")
    list_filter = ("status", "is_deleted", "created_at")
    search_fields = ("title", "url")
    date_hierarchy = "created_at"

    fieldsets = (
        (None, {"fields": ("id", "url", "title", "status")}),
        ("Result", {"fields": ("content", "error", "metadata")}),
        ("Tenant", {"fields": ("company",)}),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "created_by",
                    "updated_by",
                    "is_deleted",
                    "deleted_at",
                )
            },
        ),
    )

    def get_urls(self):
        # Custom URLs BEFORE super().get_urls() so they take precedence.
        custom = [
            path(
                "<str:pk>/run/",
                self.admin_site.admin_view(self.run_view),
                name="scrapper_scrapejob_run",
            ),
        ]
        return custom + super().get_urls()

    def run_view(self, request, pk):
        # Resolve from the permission-scoped queryset ONLY (never all_objects).
        job = get_object_or_404(self.get_queryset(request), pk=pk)
        change_url = reverse("admin:scrapper_scrapejob_change", args=[job.pk])

        if request.method == "POST":
            # CSRF is validated by CsrfViewMiddleware before we reach here.
            ScrapeService().run(job, actor=request.user)
            if job.status == ScrapeJob.Status.SUCCESS:
                self.message_user(
                    request, f"Scrape succeeded for {job.url}.", messages.SUCCESS
                )
            else:
                self.message_user(
                    request,
                    f"Scrape failed for {job.url}: {job.error}",
                    messages.WARNING,
                )
            return redirect(change_url)

        context = {
            **self.admin_site.each_context(request),
            "title": "Run scrape",
            "opts": self.model._meta,
            "original": job,
            "job": job,
            "change_url": change_url,
        }
        return TemplateResponse(
            request, "admin/scrapper/scrapejob/run.html", context
        )
