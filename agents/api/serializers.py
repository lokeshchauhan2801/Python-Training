"""Serializers for the agents API (per-field validation; 400 on bad payloads)."""

from django.db import transaction
from rest_framework import serializers

from basics.api.serializers import AuditReadOnlySerializerMixin
from chat.api.serializers import RequiredCompanyResolutionMixin

from ..models import Agent, Step, Tool


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


class AgentSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    """The flow CONTAINER. Steps live under it (see ``StepSerializer``).

    ``prompt`` is the agent's role (what it is for, which tasks it can do). It
    is read from and saved to Langfuse, never stored in the database.
    """

    prompt = serializers.CharField(
        write_only=True, required=False, allow_blank=True, trim_whitespace=True
    )

    class Meta:
        model = Agent
        fields = [
            "id",
            "slug",
            "name",
            "prompt",
            "company",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "is_deleted",
            "deleted_at",
        ]
        read_only_fields = []

    def validate_company(self, value):
        # An explicit ``company: null`` on create makes a global agent (every
        # company can use it); only a superuser may create one.
        if value is None and "company" in (self.initial_data or {}):
            if not self.context["request"].user.is_superuser:
                raise serializers.ValidationError(
                    "only an administrator can create a global agent"
                )
            return None
        return super().validate_company(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        company = self._scoped_company(attrs)

        slug = attrs.get("slug") or (self.instance and self.instance.slug)
        if slug:
            from agents.services.agent_prompts import RESERVED_PROMPT_NAMES

            # The slug is the agent's Langfuse prompt name.
            if "/" in slug:
                raise serializers.ValidationError({"slug": "an agent slug cannot contain '/'"})
            if slug in RESERVED_PROMPT_NAMES:
                raise serializers.ValidationError({"slug": f"'{slug}' is a reserved prompt name"})
        if slug:
            # The DB constraint cannot see global (NULL company) duplicates.
            clash = (
                Agent.objects.filter(company=company, slug=slug)
                if company is not None
                else Agent.objects.filter(company__isnull=True, slug=slug)
            )
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"slug": "an agent with this slug already exists"}
                )
        return attrs

    def create(self, validated_data):
        prompt = validated_data.pop("prompt", None)
        # One transaction: a prompt Langfuse rejects undoes the agent change.
        with transaction.atomic():
            agent = super().create(validated_data)
            self._save_prompt(agent, prompt)
        return agent

    def update(self, instance, validated_data):
        prompt = validated_data.pop("prompt", None)
        with transaction.atomic():
            agent = super().update(instance, validated_data)
            self._save_prompt(agent, prompt)
        return agent

    def to_representation(self, instance):
        from agents.services.agent_prompts import get_role

        data = super().to_representation(instance)
        data["prompt"] = get_role(instance)
        return data

    @staticmethod
    def _save_prompt(agent, prompt):
        if not prompt:
            return
        from agents.services.agent_prompts import save_role

        if not save_role(agent, prompt):
            raise serializers.ValidationError(
                {"prompt": "Could not save the prompt to Langfuse; try again."}
            )


class StepSerializer(
    _CompanyScopedSerializerMixin,
    AuditReadOnlySerializerMixin,
    serializers.ModelSerializer,
):
    """One node under an Agent. Holds the tools; chained via ``next_step``."""

    class Meta:
        model = Step
        fields = [
            "id",
            "agent",
            "slug",
            "name",
            "tools",
            "connected_from_start",
            "connected_to_end",
            "next_step",
            "position_x",
            "position_y",
            "order",
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
        # ``agent`` is required on create, read-only on update (a step does not
        # move between agents; re-create to move).
        if self.instance is not None and "agent" in self.fields:
            self.fields["agent"].read_only = True

    def _in_scope_tools(self):
        request = self.context["request"]
        user = request.user
        qs = Tool.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def _in_scope_steps(self):
        request = self.context["request"]
        user = request.user
        qs = Step.objects.all()
        if not user.is_superuser:
            qs = qs.filter(company__owner=user)
        return qs

    def _effective_agent(self, attrs):
        """The agent the step belongs to (create: payload; update: instance)."""
        if self.instance is not None:
            return self.instance.agent
        return attrs.get("agent")

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

    def validate_next_step(self, value):
        if value is None:
            return value
        if self.instance is not None and value.pk == self.instance.pk:
            raise serializers.ValidationError(
                "a step cannot point its next_step at itself"
            )
        if not self._in_scope_steps().filter(pk=value.pk).exists():
            raise serializers.ValidationError("not found or not owned")
        return value

    def _default_company(self, attrs):
        """On create default company from the agent (so step.company matches)."""
        agent = attrs.get("agent")
        if agent is not None:
            return agent.company
        return super()._default_company(attrs)

    def validate(self, attrs):
        # Resolve company BEFORE the mixin defaults it: on create the company
        # must come from the agent, not the user's single company.
        if self.instance is None and "company" not in attrs:
            agent = attrs.get("agent")
            if agent is not None:
                attrs["company"] = agent.company

        attrs = super().validate(attrs)

        if self.instance is None and attrs.get("agent") is None:
            raise serializers.ValidationError(
                {"agent": "an agent is required to create a step"}
            )

        agent = self._effective_agent(attrs)
        agent_company_id = getattr(agent, "company_id", None) if agent else None

        # step.company must match agent.company.
        supplied_company = attrs.get("company")
        if (
            supplied_company is not None
            and agent_company_id is not None
            and getattr(supplied_company, "pk", None) != agent_company_id
        ):
            raise serializers.ValidationError(
                {"company": "step.company must match its agent's company"}
            )
        # A step always takes its agent's company (none for a global agent).
        if self.instance is None and agent is not None:
            attrs["company"] = agent.company

        next_step = attrs.get("next_step")
        if next_step is not None and agent is not None:
            if next_step.agent_id != agent.pk:
                raise serializers.ValidationError(
                    {"next_step": "next_step must belong to the same agent"}
                )
        # A step goes on to its next step OR ends the flow, never both: the
        # runtime stops at END, so a next step after it would never run.
        if next_step is not None:
            attrs["connected_to_end"] = False
        elif attrs.get("connected_to_end"):
            attrs["next_step"] = None

        # A company agent uses its company's tools; a global agent global ones.
        tools = attrs.get("tools")
        if tools is not None and agent is not None:
            for tool in tools:
                if tool.company_id != agent_company_id:
                    raise serializers.ValidationError(
                        {"tools": "every tool must belong to the agent's company (or be global for a global agent)"}
                    )

        slug = attrs.get("slug") or (self.instance and self.instance.slug)
        if agent is not None and slug:
            clash = Step.objects.filter(agent=agent, slug=slug)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"slug": "a step with this slug already exists for the agent"}
                )
        return attrs
