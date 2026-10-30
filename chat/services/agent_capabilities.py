"""Pure deterministic capability preflight for a selected chat Agent.

This module decides, WITHOUT an LLM and WITHOUT any side effect, whether a
selected Agent is allowed to run its pipeline for the current request, must
fall back to the normal path, or must block the request:

* It performs no database writes, no LLM calls, and no tool invocation.
* It never calls ``session_files.reconcile()``, ``session_files.locked_files()``,
  a ``save()``, ``file_commands.effective_kind()`` (which reconciles), or a tool
  handler. It reads session file state only through
  ``session_files.list_files(session)`` and the read-only
  ``file_commands.effective_kind_from_files()`` helper.
* It consumes the single shared reachable-Step walker
  (``agents.services.flow.walk_reachable_steps``), so a reachability change can
  never let policy allow an action execution would not traverse.

``classify_intent`` mirrors the existing ``file_commands`` gates and the current
router prompt precedence rather than guessing with an LLM. ``preflight`` and
``preflight_for_intent`` map the classified intent against the Agent's reachable
ENABLED built-in tool slugs using the fixed policy table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chat.services import file_commands, session_files

_I = re.IGNORECASE


# --------------------------------------------------------------------------- #
# Intent + decision types
# --------------------------------------------------------------------------- #
class Intent:
    ACTION_PROCESS = "ACTION_PROCESS"
    ACTION_STOP_RESUME = "ACTION_STOP_RESUME"
    READ_STATUS = "READ_STATUS"
    READ_LIST = "READ_LIST"
    READ_SUMMARY = "READ_SUMMARY"
    READ_DOCUMENT = "READ_DOCUMENT"
    READ_EXTERNAL = "READ_EXTERNAL"
    NONE = "NONE"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str  # "pipeline" | "fallback" | "blocked"
    intent: str
    required_slugs: tuple = ()
    reason: str = ""
    reply: str = ""


# --------------------------------------------------------------------------- #
# Built-in tool slugs (registered handler-backed capabilities)
# --------------------------------------------------------------------------- #
_BUILTIN_SLUGS = frozenset(
    {
        "general_assistant",
        "document_search",
        "chat_summarise",
        "web_search",
        "add_to_knowledge_base",
        "document_processing_status",
    }
)


# --------------------------------------------------------------------------- #
# Lexical patterns (module constants, directly unit-tested)
# --------------------------------------------------------------------------- #
# Explicit conversation-summary request (precedes document classification,
# matching the router prompt's explicit-summary priority).
SUMMARY_RE = re.compile(
    r"\b(summari[sz]e|summary|recap)\b|\breview\s+this\s+(chat|conversation)\b",
    _I,
)

# Explicit document-context tokens.
DOCUMENT_TOKEN_RE = re.compile(
    r"\b(documents?|files?|attachments?|uploads?|knowledge\s*base|"
    r"company\s+documents?|library|policy)\b",
    _I,
)

# External / current-information tokens.
EXTERNAL_TOKEN_RE = re.compile(
    r"\b(web|internet|online|latest|current|today|news|search)\b",
    _I,
)


# --------------------------------------------------------------------------- #
# Policy reply + reason strings
# --------------------------------------------------------------------------- #
_NO_RUNNABLE_FLOW_REPLY = (
    "The selected agent does not have a runnable flow. Select Normal RAG or an "
    "agent with a START step, then try again."
)
_NO_RUNNABLE_FLOW_REASON = "selected_agent_has_no_runnable_flow"

_NO_PROCESS_REPLY = (
    "The selected agent does not have the Add to knowledge base tool. Select an "
    "agent that can process files, then try again."
)
_NO_STOP_RESUME_REPLY = (
    "The selected agent does not have a document processing control tool. Select "
    "an agent that can manage processing, then try again."
)


# --------------------------------------------------------------------------- #
# Intent classification
# --------------------------------------------------------------------------- #
def classify_intent(message_text, session, attachment_ids, selected_entry_ids=()):
    """Classify the request intent with the exact design precedence."""
    message_text = message_text or ""
    attachment_ids = list(attachment_ids or [])
    selected_entry_ids = list(selected_entry_ids or [])

    command = file_commands.classify(message_text)

    # 1. A validated picker selection with an empty message or a PROCESS command
    #    means "process those rows" — decided before the effective-kind helper.
    if selected_entry_ids and (
        not message_text.strip() or command.kind == file_commands.Kind.PROCESS
    ):
        return Intent.ACTION_PROCESS

    # 2. The read-only effective-kind helper (never reconciles / writes).
    files = session_files.list_files(session)
    kind = file_commands.effective_kind_from_files(command, session, files)
    if kind == file_commands.Kind.PROCESS:
        return Intent.ACTION_PROCESS
    if kind in (file_commands.Kind.STOP, file_commands.Kind.RESUME):
        return Intent.ACTION_STOP_RESUME
    if kind == file_commands.Kind.STATUS:
        return Intent.READ_STATUS
    if kind == file_commands.Kind.LIST:
        return Intent.READ_LIST

    # 3. Explicit conversation-summary request.
    if SUMMARY_RE.search(message_text):
        return Intent.READ_SUMMARY

    # 4. READ_DOCUMENT: a chart with a current attachment or a file chart target;
    #    any current attachment paired with a non-empty question; or an explicit
    #    document-context token.
    from chat.services.chat_service import ChatService

    is_chart = ChatService._is_chart_request(message_text)
    has_attachment = bool(attachment_ids)
    if is_chart and has_attachment:
        return Intent.READ_DOCUMENT
    target = ChatService._chart_target(session, message_text)
    if target is not None and getattr(target, "kind", None) == "file":
        return Intent.READ_DOCUMENT
    if has_attachment and message_text.strip():
        return Intent.READ_DOCUMENT
    if DOCUMENT_TOKEN_RE.search(message_text):
        return Intent.READ_DOCUMENT

    # 5. READ_EXTERNAL.
    if EXTERNAL_TOKEN_RE.search(message_text):
        return Intent.READ_EXTERNAL

    # 6. Everything else.
    return Intent.NONE


# --------------------------------------------------------------------------- #
# Capability aggregation
# --------------------------------------------------------------------------- #
def _reachable_enabled_slugs(agent) -> set:
    """Built-in slugs enabled on the Agent's REACHABLE Steps.

    Uses the shared walker so orphan/unreachable Steps (which never execute)
    cannot satisfy a capability. A built-in slug counts even when its Tool row
    is type ``langfuse`` because ``_enabled_for`` resolves a registered slug to
    its handler; the decisive contract is the registered slug.
    """
    from agents.services.flow import walk_reachable_steps

    slugs: set = set()
    for step in walk_reachable_steps(agent):
        for tool in step.tools.filter(is_enabled=True):
            if tool.slug in _BUILTIN_SLUGS:
                slugs.add(tool.slug)
    return slugs


# Policy table: intent -> (required slug set [any-of], outcome-class, reply/reason)
# outcome-class "write" => blocked when missing; "read" => fallback when missing.
def _policy_for_intent(intent, available):
    """Return (outcome, required_slugs, reason, reply) for ``intent``."""
    if intent == Intent.ACTION_PROCESS:
        required = ("add_to_knowledge_base",)
        if "add_to_knowledge_base" in available:
            return "pipeline", required, "", ""
        return "blocked", required, "missing add_to_knowledge_base capability", _NO_PROCESS_REPLY
    if intent == Intent.ACTION_STOP_RESUME:
        required = ("add_to_knowledge_base", "document_processing_status")
        if available & set(required):
            return "pipeline", required, "", ""
        return (
            "blocked",
            required,
            "missing document processing control capability",
            _NO_STOP_RESUME_REPLY,
        )
    if intent == Intent.READ_STATUS:
        required = ("document_processing_status",)
        if "document_processing_status" in available:
            return "pipeline", required, "", ""
        return "fallback", required, "missing enabled document_processing_status on reachable steps", ""
    if intent == Intent.READ_LIST:
        required = ("document_processing_status", "add_to_knowledge_base")
        if available & set(required):
            return "pipeline", required, "", ""
        return "fallback", required, "missing enabled document list capability on reachable steps", ""
    if intent == Intent.READ_SUMMARY:
        required = ("chat_summarise",)
        if "chat_summarise" in available:
            return "pipeline", required, "", ""
        return "fallback", required, "missing enabled chat_summarise on reachable steps", ""
    if intent == Intent.READ_DOCUMENT:
        required = ("document_search",)
        if "document_search" in available:
            return "pipeline", required, "", ""
        return "fallback", required, "missing enabled document_search on reachable steps", ""
    if intent == Intent.READ_EXTERNAL:
        required = ("web_search",)
        if "web_search" in available:
            return "pipeline", required, "", ""
        return "fallback", required, "missing enabled web_search on reachable steps", ""
    # Intent.NONE: the selected Agent runs its own Step routers normally.
    return "pipeline", (), "", ""


# --------------------------------------------------------------------------- #
# Preflight entry points
# --------------------------------------------------------------------------- #
def _no_runnable_flow(agent) -> bool:
    from agents.services.flow import walk_reachable_steps

    return not walk_reachable_steps(agent)


def preflight(agent, session, message_text, attachment_ids, selected_entry_ids=()):
    """Decide pipeline / fallback / blocked for a classified chat request."""
    intent = classify_intent(
        message_text, session, attachment_ids, selected_entry_ids
    )
    if _no_runnable_flow(agent):
        return PolicyDecision(
            outcome="blocked",
            intent=intent,
            required_slugs=(),
            reason=_NO_RUNNABLE_FLOW_REASON,
            reply=_NO_RUNNABLE_FLOW_REPLY,
        )
    available = _reachable_enabled_slugs(agent)
    outcome, required, reason, reply = _policy_for_intent(intent, available)
    return PolicyDecision(
        outcome=outcome,
        intent=intent,
        required_slugs=tuple(required),
        reason=reason,
        reply=reply,
    )


def preflight_for_intent(agent, session, intent):
    """Capability check for an endpoint action intent (no lexical classify).

    Accepts only ``ACTION_PROCESS`` or ``ACTION_STOP_RESUME``; any other value
    raises ``ValueError`` for programmer misuse. Uses the same reachable-chain
    check and action-row mapping as :func:`preflight`, returning only
    ``pipeline`` or ``blocked``.
    """
    if intent not in (Intent.ACTION_PROCESS, Intent.ACTION_STOP_RESUME):
        raise ValueError(f"preflight_for_intent does not accept intent {intent!r}")
    if _no_runnable_flow(agent):
        return PolicyDecision(
            outcome="blocked",
            intent=intent,
            required_slugs=(),
            reason=_NO_RUNNABLE_FLOW_REASON,
            reply=_NO_RUNNABLE_FLOW_REPLY,
        )
    available = _reachable_enabled_slugs(agent)
    outcome, required, reason, reply = _policy_for_intent(intent, available)
    return PolicyDecision(
        outcome=outcome,
        intent=intent,
        required_slugs=tuple(required),
        reason=reason,
        reply=reply,
    )
