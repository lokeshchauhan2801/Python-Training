"""Each Agent's runtime config, kept only in Langfuse.

The config is the ``config`` JSON of the agent's Langfuse prompt (see
``agent_prompts``), so Langfuse versions it together with the role text and a
rollback there rolls back both. Nothing is stored in the database::

    {
      "schema_version": 1,
      "provider": "openai",
      "model": "gpt-4o-mini",
      "temperature": 1.0,
      "max_tokens": 8192,
      "retrieval": {"top_k": 5}
    }

Defaults and limits come from settings (env), never from the database:

* ``defaults()`` is what a new agent starts with (the canvas prefills it).
* ``validate()`` is strict: the API rejects a bad value with a field error.
* ``resolve()`` is lenient: at run time a missing or bad value (e.g. a typo
  made in the Langfuse UI) falls back to its default, so a chat turn never
  fails on config. The model must be one the provider allows in
  ``settings.LLM_PROVIDERS``, so a config cannot switch on an unapproved model.

Secrets (API keys, base URLs) never go in the config; they stay in env.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Keys this module owns. Other keys someone adds in Langfuse are kept on save
# and ignored at run time.
_KNOWN_KEYS = {
    "schema_version",
    "provider",
    "model",
    "temperature",
    "max_tokens",
    "retrieval",
}
_RETRIEVAL_KEYS = {"top_k"}
# Retired keys older Langfuse versions may still hold: dropped without error.
_RETIRED_RETRIEVAL_KEYS = {"chart_top_k"}

TEMPERATURE_RANGE = (0.0, 2.0)


class ConfigError(ValueError):
    """A config value the API must reject; ``errors`` maps field -> message."""

    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _providers() -> dict:
    return getattr(settings, "LLM_PROVIDERS", {}) or {}


def _provider_models(provider: str) -> list[str]:
    cfg = _providers().get(provider) or {}
    models = list(cfg.get("models") or [])
    default = cfg.get("chat_model")
    if default and default not in models:
        models.insert(0, default)
    return models


def _default_provider_model() -> tuple[str, str]:
    from basics.llm import resolve_provider_model

    return resolve_provider_model(None, settings.LLM_PROVIDER)


def limits() -> dict:
    return {
        "temperature": list(TEMPERATURE_RANGE),
        "max_tokens": [1, settings.AGENT_MAX_TOKENS_LIMIT],
        "top_k": [1, settings.AGENT_TOP_K_LIMIT],
    }


def model_options() -> list[dict]:
    """Every allowed ``{provider, model}`` pair (the canvas model picker)."""
    return [
        {"provider": provider, "model": model}
        for provider in _providers()
        for model in _provider_models(provider)
    ]


def defaults() -> dict:
    """A new agent's config: how chat runs today without an agent."""
    provider, model = _default_provider_model()
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "model": model,
        "temperature": settings.AGENT_DEFAULT_TEMPERATURE,
        "max_tokens": settings.AGENT_DEFAULT_MAX_TOKENS,
        "retrieval": {"top_k": settings.RAG_TOP_K},
    }


def _number(value, kind, low, high):
    """``value`` as ``kind`` within [low, high], else raise ValueError."""
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("a number is required")
    try:
        number = kind(value)
    except (TypeError, ValueError):
        raise ValueError("a number is required") from None
    if kind is int and float(value) != number:
        raise ValueError("a whole number is required")
    if not low <= number <= high:
        raise ValueError(f"must be between {low} and {high}")
    return number


def _check(raw: dict) -> tuple[dict, dict]:
    """Validate ``raw`` field by field -> ``(clean, errors)``.

    ``clean`` holds only the valid known fields; ``errors`` maps the dotted
    field name to a message. Missing fields are neither clean nor errors.
    """
    clean, errors = {}, {}
    lim = limits()

    provider = raw.get("provider")
    model = raw.get("model")
    if provider is not None or model is not None:
        if provider is None:
            from basics.llm import resolve_provider_model

            provider = resolve_provider_model(model, None)[0]
        if provider not in _providers():
            errors["provider"] = f"unknown provider '{provider}'"
        else:
            allowed = _provider_models(provider)
            if model is None:
                model = allowed[0] if allowed else None
            if model not in allowed:
                errors["model"] = (
                    f"'{model}' is not an allowed {provider} model "
                    f"({', '.join(allowed)})"
                )
            else:
                clean["provider"], clean["model"] = provider, model

    if "temperature" in raw:
        try:
            clean["temperature"] = _number(
                raw["temperature"], float, *lim["temperature"]
            )
        except ValueError as exc:
            errors["temperature"] = str(exc)

    if "max_tokens" in raw:
        try:
            clean["max_tokens"] = _number(raw["max_tokens"], int, *lim["max_tokens"])
        except ValueError as exc:
            errors["max_tokens"] = str(exc)

    retrieval = raw.get("retrieval")
    if retrieval is not None:
        if not isinstance(retrieval, dict):
            errors["retrieval"] = "must be an object"
        else:
            clean_retrieval = {}
            for key in _RETRIEVAL_KEYS:
                if key in retrieval:
                    try:
                        clean_retrieval[key] = _number(
                            retrieval[key], int, *lim[key]
                        )
                    except ValueError as exc:
                        errors[f"retrieval.{key}"] = str(exc)
            unknown = set(retrieval) - _RETRIEVAL_KEYS - _RETIRED_RETRIEVAL_KEYS
            if unknown:
                errors["retrieval"] = f"unknown keys: {', '.join(sorted(unknown))}"
            if clean_retrieval:
                clean["retrieval"] = clean_retrieval
    return clean, errors


def _merge(base: dict, clean: dict) -> dict:
    merged = {**base, **{k: v for k, v in clean.items() if k != "retrieval"}}
    merged["retrieval"] = {
        **(base.get("retrieval") or {}),
        **(clean.get("retrieval") or {}),
    }
    merged["schema_version"] = SCHEMA_VERSION
    return merged


def validate(raw, base: dict | None = None) -> dict:
    """Strict: ``raw`` merged over ``base`` (default: ``defaults()``).

    ``raw`` may be partial. Raises ``ConfigError`` on any bad or unknown field.
    """
    if not isinstance(raw, dict):
        raise ConfigError({"config": "must be an object"})
    clean, errors = _check(raw)
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        errors["config"] = f"unknown keys: {', '.join(sorted(unknown))}"
    if errors:
        raise ConfigError(errors)
    return _merge(base if base is not None else defaults(), clean)


def resolve(raw) -> dict:
    """Lenient: the stored config with each missing or bad field defaulted."""
    raw = raw if isinstance(raw, dict) else {}
    clean, errors = _check(raw)
    if errors:
        logger.warning("agent config has invalid fields, using defaults: %s", errors)
    return _merge(defaults(), clean)
