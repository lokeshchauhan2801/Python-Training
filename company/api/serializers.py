"""Serializers for the company API."""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin

from ..models import Company


class CompanySerializer(AuditReadOnlySerializerMixin, serializers.ModelSerializer):
    """Company I/O. ``owner``/audit are server-controlled on write."""


    SERVER_CONTROLLED = AuditReadOnlySerializerMixin.SERVER_CONTROLLED + ["owner"]

    class Meta:
        model = Company
        fields = [
            "id",
            "owner",
            "name",
            "slug",
            "description",
            "is_active",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]
