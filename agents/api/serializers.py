"""Serializers for the agents API (per-field validation; 400 on bad payloads)."""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin
from chat.api.serializers import RequiredCompanyResolutionMixin

from ..models import Agent, Tool


class _CompanyScopedSerializerMixin(RequiredCompanyResolutionMixin):
    """Shared ``company`` handling for every agents serializer.

    ``company`` is writable on create (validated against ownership via
    ``_resolve_company``), read-only on update, and never forced on the client.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None and "company" in self.fields:
            self.fields["company"].read_only = True
        if "company" in self.fields:
            self.fields["company"].required = False

        from rest_framework.validators import UniqueTogetherValidator

        self.validators = [
            v
            for v in self.validators
            if not isinstance(v, UniqueTogetherValidator)
        ]

    def validate_company(self, value):
        return self._resolve_company(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None and "company" not in attrs:
            attrs["company"] = self._default_company(attrs)
        return attrs

    def _default_company(self, attrs):
        """Company to use when the client omits one on create.

        Default: the user's active/single owned company (``_resolve_company``).
        """
        return self._resolve_company(None)

    def _scoped_company(self, attrs):
        """Resolve the effective company for cross-field validation.

        On create it is ``attrs["company"]`` (already resolved); on update it is
        the instance's company (``company`` is read-only on update).
        """
        if self.instance is not None:
            return self.instance.company
        return attrs.get("company")


class ToolSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = Tool
        fields = [
            "id",
            "slug",
            "name",
            "type",
            "description",
            "is_enabled",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)

        slug = attrs.get("slug") or (self.instance and self.instance.slug)
        tool_type = attrs.get("type") or (self.instance and self.instance.type)

        from chat.services.langgraph_service import available_slugs

        if (
            tool_type != Tool.TYPE_LANGFUSE
            and slug
            and slug not in available_slugs()
        ):
            raise serializers.ValidationError(
                {"slug": f"Unknown tool slug '{slug}' (no handler registered)."}
            )
        return attrs


class AgentSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = Agent
        fields = [
            "id",
            "slug",
            "name",
            "tools",
            "connected_from_start",
            "connected_to_end",
            "next_node",
            "position_x",
            "position_y",
            "order",
            "is_active",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]
        read_only_fields = ["is_active"]

    def _in_scope_tools(self):
        request = self.context["request"]
        user = request.user
        qs = Tool.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def _in_scope_agents(self):
        request = self.context["request"]
        user = request.user
        qs = Agent.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def validate_tools(self, value):
        in_scope = set(
            self._in_scope_tools()
            .filter(pk__in=[t.pk for t in value])
            .values_list("pk", flat=True)
        )
        for tool in value:
            if tool.pk not in in_scope:
                raise serializers.ValidationError(
                    f"Tool '{tool.pk}' not found or not owned."
                )
        return value

    def validate_next_node(self, value):
        if value is None:
            return value
        if self.instance is not None and value.pk == self.instance.pk:
            raise serializers.ValidationError(
                "an agent cannot point its next_node at itself"
            )
        if not self._in_scope_agents().filter(pk=value.pk).exists():
            raise serializers.ValidationError("not found or not owned")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        company = self._scoped_company(attrs)

        next_node = attrs.get("next_node")
        if (
            next_node is not None
            and company is not None
            and next_node.company_id != getattr(company, "pk", None)
        ):
            raise serializers.ValidationError(
                {"next_node": "next_node must belong to the agent's company"}
            )

        tools = attrs.get("tools")
        if tools is not None and company is not None:
            for tool in tools:
                if tool.company_id != getattr(company, "pk", None):
                    raise serializers.ValidationError(
                        {"tools": "every tool must belong to the agent's company"}
                    )

        slug = attrs.get("slug") or (self.instance and self.instance.slug)
        if company is not None and slug:
            clash = Agent.objects.filter(company=company, slug=slug)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"slug": "an agent with this slug already exists for the company"}
                )
        return attrs
