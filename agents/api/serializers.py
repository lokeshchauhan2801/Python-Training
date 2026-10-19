"""Serializers for the agents API (per-field validation; 400 on bad payloads)."""

from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin
from chat.api.serializers import RequiredCompanyResolutionMixin

from ..models import AgentEdge, AgentGraph, AgentNode, Tool
from ..tools import available_slugs


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

        # Drop DRF's auto-generated UniqueTogetherValidators. Our models use
        # PARTIAL unique constraints (``condition=Q(is_deleted=False)``), which
        # DRF turns into a validator whose ``condition_fields`` include
        # ``is_deleted``. On a PARTIAL update (PATCH) ``is_deleted`` is not in
        # the submitted data, so the validator raises ``KeyError: 'is_deleted'``
        # (DRF validators.py line 191) and 500s when you Save a node. Uniqueness
        # is still enforced two other ways: our own per-graph slug check in
        # ``AgentNodeSerializer.validate`` and the DB partial unique constraints
        # themselves. So removing only these auto-built validators is safe.
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
            attrs["company"] = self._resolve_company(None)
        return attrs

    def _scoped_company(self, attrs):
        """Resolve the effective company for cross-field validation.

        On create it is ``attrs["company"]`` (already resolved); on update it is
        the instance's company (``company`` is read-only on update).
        """
        if self.instance is not None:
            return self.instance.company
        return attrs.get("company")


class AgentGraphSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = AgentGraph
        fields = [
            "id",
            "name",
            "is_active",
            "description",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def validate_name(self, value):
        # Required non-blank on create (<=200 is enforced by the model field).
        if self.instance is None and not (value or "").strip():
            raise serializers.ValidationError("This field may not be blank.")
        return value


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
            "description",
            "is_enabled",
            "config",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def validate_slug(self, value):
        if value not in available_slugs():
            raise serializers.ValidationError(
                f"Unknown tool slug '{value}' (no handler registered)."
            )
        return value

    def validate_config(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("config must be an object.")
        return value


class AgentNodeSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = AgentNode
        fields = [
            "id",
            "graph",
            "slug",
            "kind",
            "label",
            "tools",
            "prompt_name",
            "position",
            "config",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def validate_position(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("position must be an object.")
        return value

    def _in_scope_graphs(self):
        request = self.context["request"]
        user = request.user
        qs = AgentGraph.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def _in_scope_tools(self):
        request = self.context["request"]
        user = request.user
        qs = Tool.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def validate_graph(self, value):
        if not self._in_scope_graphs().filter(pk=value.pk).exists():
            raise serializers.ValidationError("not found or not owned")
        return value

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

    def validate(self, attrs):
        attrs = super().validate(attrs)
        graph = attrs.get("graph") or (self.instance and self.instance.graph)
        # A node's company is ALWAYS its graph's company. The shared mixin
        # defaults an omitted company to the user's ACTIVE/single company, which
        # is wrong for a multi-company user (it picks a company unrelated to the
        # chosen graph and then trips the mismatch check below). So on create,
        # override the resolved company with the graph's company before the
        # check runs — the client never needs to send a correct company for a
        # node, and a stale active-company guess can't block creation.
        if self.instance is None and graph is not None:
            attrs["company"] = graph.company
        company = self._scoped_company(attrs)
        # The node's company must match its graph's company (model invariant;
        # surfaced here as a 400 rather than a 500 on save).
        if graph is not None and company is not None and graph.company_id != getattr(
            company, "pk", None
        ):
            raise serializers.ValidationError(
                {"company": "node company must match graph company"}
            )
        # Every attached tool must belong to the same company as the node.
        tools = attrs.get("tools")
        if tools is not None and company is not None:
            for tool in tools:
                if tool.company_id != getattr(company, "pk", None):
                    raise serializers.ValidationError(
                        {"tools": "every tool must belong to the node's company"}
                    )
        # Unique slug within the graph (serializer-level; the DB partial unique
        # constraint is the backstop).
        slug = attrs.get("slug") or (self.instance and self.instance.slug)
        if graph is not None and slug:
            clash = AgentNode.objects.filter(graph=graph, slug=slug)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"slug": "a node with this slug already exists in the graph"}
                )
        return attrs


class AgentEdgeSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    class Meta:
        model = AgentEdge
        fields = [
            "id",
            "graph",
            "source",
            "target",
            "condition",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]

    def _in_scope_graphs(self):
        request = self.context["request"]
        user = request.user
        qs = AgentGraph.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def _in_scope_nodes(self):
        request = self.context["request"]
        user = request.user
        qs = AgentNode.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def validate_graph(self, value):
        if not self._in_scope_graphs().filter(pk=value.pk).exists():
            raise serializers.ValidationError("not found or not owned")
        return value

    def validate_source(self, value):
        if not self._in_scope_nodes().filter(pk=value.pk).exists():
            raise serializers.ValidationError("not found or not owned")
        return value

    def validate_target(self, value):
        if not self._in_scope_nodes().filter(pk=value.pk).exists():
            raise serializers.ValidationError("not found or not owned")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        source = attrs.get("source") or (self.instance and self.instance.source)
        target = attrs.get("target") or (self.instance and self.instance.target)
        graph = attrs.get("graph") or (self.instance and self.instance.graph)
        # An edge's company is ALWAYS its graph's company (same reasoning as
        # AgentNode.validate): override the mixin's active-company default on
        # create so a multi-company user's stale active company can't block
        # wiring an edge.
        if self.instance is None and graph is not None:
            attrs["company"] = graph.company
        # Self-loop rejected.
        if source is not None and target is not None and source.pk == target.pk:
            raise serializers.ValidationError(
                {"target": "an edge cannot connect a node to itself"}
            )
        # Source/target must be in the same graph (no cross-graph edge).
        if (
            source is not None
            and target is not None
            and source.graph_id != target.graph_id
        ):
            raise serializers.ValidationError(
                {"target": "an edge must connect nodes of the same graph"}
            )
        # The edge's graph must match the endpoints' graph.
        if graph is not None and source is not None and source.graph_id != graph.pk:
            raise serializers.ValidationError(
                {"graph": "edge graph must match the source/target graph"}
            )
        return attrs
