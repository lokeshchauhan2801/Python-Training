"""Serializers for the auth (``accounts``) API."""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin

from ..models import User


class UserSerializer(AuditReadOnlySerializerMixin, serializers.ModelSerializer):
    """Read shape for ``accounts.User``; the password is never exposed."""

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "username",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "created_at",
            "updated_at",
            "is_deleted",
            "deleted_at",
        ]
