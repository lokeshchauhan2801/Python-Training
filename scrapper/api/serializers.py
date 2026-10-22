"""Serializers for the scrapper API.

``ScrapeJobSerializer`` reuses the chat required-company resolution rule so the
required ``company`` FK is resolved against request-user ownership.
"""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin
from chat.api.serializers import RequiredCompanyResolutionMixin

from ..models import ScrapeJob


class ScrapeJobSerializer(
    RequiredCompanyResolutionMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = ScrapeJob
        fields = [
            "id",
            "url",
            "title",
            "status",
            "content",
            "error",
            "metadata",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None and "company" in self.fields:
            self.fields["company"].read_only = True
        if "company" in self.fields:
            self.fields["company"].required = False


        for name in ("content", "error"):
            if name in self.fields:
                self.fields[name].read_only = True

    def validate_company(self, value):
        return self._resolve_company(value)

    def validate(self, attrs):
        if self.instance is None and "company" not in attrs:
            attrs["company"] = self._resolve_company(None)
        return attrs


class RunSerializer(serializers.Serializer):
    """No-body input for the ``run`` action (placeholder for a clean contract)."""
