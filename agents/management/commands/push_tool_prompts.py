"""push_tool_prompts — create/version the built-in tool prompts in Langfuse.

The app only READS prompts from Langfuse (handlers call get_prompt(name) and
fall back to a built-in string when the prompt is absent). So a fresh Langfuse
project shows an EMPTY Prompts page — that is expected, not a bug. This command
seeds the four built-in tool prompts into Langfuse (labelled "production", which
is the version get_prompt serves) so they appear in the portal and can then be
edited there. Re-running creates a new version each time (Langfuse versions
prompts), so it is safe to run repeatedly; it never deletes a user's edits.

Requires the LANGFUSE_* keys in the server env (source ~/.bash_profile). With no
keys it is a graceful no-op that reports nothing was pushed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from basics.langfuse import get_langfuse_client
from chat.services import _SUMMARY_SYSTEM_PROMPT

# name -> default text. Names match the conventional Tool.prompt_name values and
# the fallback strings the handlers use, so pushing them makes the portal
# reflect exactly what the code does today.
_DEFAULT_PROMPTS = {
    "tool_chat_summarise": _SUMMARY_SYSTEM_PROMPT,
    "tool_general_assistant": (
        "You are a helpful general assistant. Answer the user's question "
        "clearly and concisely using your general knowledge."
    ),
    "tool_document_search": (
        "Answer strictly from the retrieved document passages below. Cite the "
        "sources you use and do not invent facts."
    ),
    "tool_web_search": (
        "You are a web-search assistant. Summarise the most relevant findings "
        "for the user's query."
    ),
}


class Command(BaseCommand):
    help = (
        "Create/version the built-in tool prompts in Langfuse (labelled "
        "production) so they appear in the Langfuse Prompts portal."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            dest="only",
            default=None,
            help="Push only this prompt name (e.g. tool_chat_summarise).",
        )

    def handle(self, *args, **options):
        client = get_langfuse_client()
        if not client.configured:
            self.stdout.write(
                self.style.WARNING(
                    "Langfuse is not configured (LANGFUSE_* keys missing). "
                    "Source ~/.bash_profile and retry. Nothing pushed."
                )
            )
            return

        only = options.get("only")
        items = (
            {only: _DEFAULT_PROMPTS[only]}
            if only and only in _DEFAULT_PROMPTS
            else _DEFAULT_PROMPTS
        )
        if only and only not in _DEFAULT_PROMPTS:
            self.stdout.write(
                self.style.ERROR(
                    f"Unknown prompt '{only}'. Known: "
                    + ", ".join(sorted(_DEFAULT_PROMPTS))
                )
            )
            return

        pushed = 0
        for name, text in items.items():
            ok = client.create_prompt(name, text, labels=["production"])
            if ok:
                pushed += 1
                self.stdout.write(f"  + pushed {name}")
            else:
                self.stdout.write(
                    self.style.ERROR(f"  ! failed to push {name}")
                )
        client.flush()
        self.stdout.write(
            self.style.SUCCESS(
                f"Pushed {pushed}/{len(items)} prompt(s) to Langfuse "
                "(label=production). They now appear in the Prompts portal."
            )
        )
