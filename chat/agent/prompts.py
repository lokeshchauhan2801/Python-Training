"""The Agent's system prompt.

The agent's role (its Langfuse prompt) leads, then a line naming the real
provider and model, the plain-text formatting rule, and how to use tools.
Each tool's own description (``Tool.description``) reaches the model with the
tool's schema, so it is not repeated here.
"""

_PROVIDER_DISPLAY = {"openai": "OpenAI", "deepseek": "DeepSeek"}

PLAIN_TEXT_GUIDANCE = (
    "Format every visible response as plain text. Use normal sentences, dot "
    "bullets, or numbered lists. Do not use Markdown emphasis or asterisk bullets."
)

_TOOL_GUIDANCE = (
    "You can call the tools you are given, one or more times, before you "
    "answer. Call a tool only when it is needed; answer general questions "
    "directly. Use document_search only when the user asks about company data, "
    "documents or files, not merely because files exist. When an answer uses "
    "tool results, base it on them and cite passages by their [n] number. "
    "Never put chart data or JSON in your reply; a chart tool shows the chart, "
    "so reply with a short caption."
)


def identity_line(llm) -> str:
    """A system line stating the REAL provider/model serving this turn.

    Models are unreliable about their own identity (e.g. DeepSeek often claims
    to be OpenAI GPT because of its training data), so the ground truth is
    injected.
    """
    provider = getattr(llm, "provider", "") or ""
    model = getattr(llm, "model", "") or ""
    display = _PROVIDER_DISPLAY.get(provider, provider.capitalize() or "the configured provider")
    return (
        "Your identity: you are served by the "
        f"{display} API using the model '{model}'. If the user asks which model, "
        "provider, or LLM you are, answer with exactly this provider and model "
        f"name ({display}, model '{model}'). Do NOT claim to be OpenAI, ChatGPT, "
        "GPT-3.5/GPT-4, or any other provider/model unless that is the provider "
        "and model named here, even if your training data suggests otherwise."
    )


def system_prompt(role: str, llm, has_tools: bool) -> str:
    parts = [(role or "").strip(), identity_line(llm), PLAIN_TEXT_GUIDANCE]
    if has_tools:
        parts.append(_TOOL_GUIDANCE)
    return "\n\n".join(p for p in parts if p)
