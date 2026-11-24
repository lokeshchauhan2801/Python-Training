"""Keep each Agent's Langfuse role prompt and graph JSON in step with its rows.

``Agent.graph`` (what chat runs) is rewritten whenever a Step, a Step's tools,
a Tool or the Agent's slug changes.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from agents.models import Agent, Step, Tool

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Agent)
def remember_prompt_name(sender, instance, **kwargs):
    """Note the old prompt name so a slug change can carry the role over."""
    from agents.services.agent_prompts import prompt_name

    old = Agent.all_objects.filter(pk=instance.pk).select_related("company").first()
    instance._old_prompt_name = prompt_name(old) if old else None


@receiver(post_save, sender=Agent)
def create_agent_prompt(sender, instance, created, **kwargs):
    """Covers every way an Agent is made or renamed: canvas, API, admin, seeds.

    Runs after commit and never raises, so Langfuse being down cannot fail
    saving the Agent.
    """
    from agents.services import agent_prompts

    old_name = getattr(instance, "_old_prompt_name", None)
    if not created and old_name == agent_prompts.prompt_name(instance):
        return

    def _push():
        try:
            client = agent_prompts._client()
            text, config = (
                client.get_prompt_and_config(old_name, fallback="")
                if old_name
                else ("", {})
            )
            agent_prompts.ensure_role(
                instance, text or None, langfuse=client, config=config or None
            )
        except Exception:
            logger.exception("could not create Langfuse prompt for agent %s", instance.pk)

    transaction.on_commit(_push)


# --------------------------------------------------------------------------- #
# Agent.graph sync
# --------------------------------------------------------------------------- #
def _sync(agent_ids):
    from agents.services.graph_json import sync_agent_graph

    for agent in Agent.all_objects.filter(pk__in=set(agent_ids)):
        sync_agent_graph(agent)


@receiver(post_save, sender=Agent)
def sync_graph_on_agent_save(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    # A renamed agent keeps its graph's "agent" in step.
    if (instance.graph or {}).get("agent") not in (None, instance.slug_name):
        _sync([instance.pk])


@receiver(post_save, sender=Step)
@receiver(post_delete, sender=Step)
def sync_graph_on_step(sender, instance, raw=False, **kwargs):
    if not raw and instance.agent_id:
        _sync([instance.agent_id])


@receiver(m2m_changed, sender=Step.tools.through)
def sync_graph_on_step_tools(sender, instance, action, reverse, pk_set, **kwargs):
    if not action.startswith("post_"):
        return
    if reverse:  # tool.steps.add(...): instance is a Tool
        ids = Step.all_objects.filter(pk__in=pk_set or []).values_list("agent_id", flat=True)
        if action == "post_clear":
            ids = Agent.all_objects.values_list("pk", flat=True)
        _sync(ids)
    else:
        _sync([instance.agent_id])


@receiver(post_save, sender=Tool)
@receiver(post_delete, sender=Tool)
def sync_graph_on_tool(sender, instance, raw=False, **kwargs):
    """Enabling, disabling or deleting a tool changes the graphs using it."""
    if raw:
        return
    _sync(Step.all_objects.filter(tools=instance).values_list("agent_id", flat=True))
