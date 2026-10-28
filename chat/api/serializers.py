"""Serializers for the chat API.

``ConversationSessionSerializer`` owns the required-``company`` resolution rule
(design "Required-company resolution on create"):

* a supplied ``company`` id must be owned by the request user (superuser: any),
  else HTTP 400;
* an omitted ``company`` defaults to the user's single owned company; zero or
  more than one owned company is a 400.
"""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin
from company.models import Company

from ..models import Conversation, ConversationSession


class RequiredCompanyResolutionMixin:
    """Resolve the required ``company`` FK against request-user ownership."""

    def _resolve_company(self, supplied):
        request = self.context["request"]
        user = request.user
        base = Company.objects.all() if user.is_superuser else Company.objects.filter(
            owner=user
        )
        if supplied is not None:
            if not base.filter(pk=supplied.pk).exists():
                raise serializers.ValidationError(
                    {"company": "not found or not owned"}
                )
            return supplied
        owned = list(base[:2])
        if not owned:
            raise serializers.ValidationError(
                {"company": "you must create a company first"}
            )
        if len(owned) > 1:
            raise serializers.ValidationError(
                {"company": "ambiguous; specify a company"}
            )
        return owned[0]


class ConversationSessionSerializer(
    RequiredCompanyResolutionMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    # The session PK is now the ULID field ``session_id``. We keep the external
    # API key ``id`` (sourced from ``session_id``) so existing API/frontend
    # consumers that read ``session.id`` keep working unchanged.
    id = serializers.CharField(source="session_id", read_only=True)

    class Meta:
        model = ConversationSession
        fields = [
            "id",
            "title",
            "user",
            "company",
            "context",
            "metadata",
            "summary",
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

    def validate_company(self, value):
        return self._resolve_company(value)

    def validate(self, attrs):


        if self.instance is None and "company" not in attrs:
            attrs["company"] = self._resolve_company(None)
        return attrs


class ConversationSerializer(
    AuditReadOnlySerializerMixin, serializers.ModelSerializer
):
    # ``chart_data`` and ``citations`` no longer have their own columns; they
    # live in ``context`` under the reserved keys ``"chart"``/``"citations"``.
    # We keep the same external API keys so the frontend contract is unchanged.
    chart_data = serializers.SerializerMethodField()
    citations = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "session",
            "user",
            "company",
            "model_name",
            "question",
            "response",
            "context",
            "metadata",
            "citations",
            "chart_data",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def get_chart_data(self, obj):
        return obj.context.get("chart")

    def get_citations(self, obj):
        return obj.context.get("citations", [])


class AskSerializer(serializers.Serializer):
    """Input shape for the ``ask`` endpoint."""

    session = serializers.CharField(max_length=26)
    question = serializers.CharField()
