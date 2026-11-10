"""Keep each Agent's Langfuse role prompt in step with the Agent row."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from agents.models import Agent

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
