"""Choosing a provider, and the rule that keeps existing installs working.

**An unset `LLM_PROVIDER` is inferred from whichever key is present**, and the
order puts OpenAI first. That is not a preference — it is today's behaviour
preserved exactly, the same discipline that made `RBAC_DEFAULT_ROLE` default to
`admin` and kept the single-process job store as the default. Nobody who set
`OPENAI_API_KEY` and nothing else should discover at upgrade time that their
investigations now go somewhere different, or nowhere at all.

Naming a provider explicitly turns inference off, so a deployment with both keys
set (migrating from one to the other, say) gets what it asked for rather than
what happened to be first in a list. An unknown name is **refused at startup**
alongside every other setting, because the alternative is a service that boots,
answers `/health`, and falls back to the deterministic diagnosis on every
investigation while the operator wonders why the model never runs.
"""

from app.ai.providers.anthropic import DEFAULT_BASE_URL as ANTHROPIC_URL
from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.base import LLMProvider
from app.ai.providers.openai import DEFAULT_BASE_URL as OPENAI_URL
from app.ai.providers.openai import OpenAICompatibleProvider, OpenAIProvider

PROVIDERS = ("openai", "anthropic", "compatible")


def provider_names() -> tuple[str, ...]:
    return PROVIDERS


def resolve_name(config) -> str:
    """The configured provider, or the one implied by the keys that are set."""
    named = (config.llm_provider or "").strip().lower()
    if named:
        return named
    if config.openai_api_key:
        return "openai"
    if config.anthropic_api_key:
        return "anthropic"
    if config.llm_base_url:
        return "compatible"
    # Nothing configured at all. `openai` is the honest answer: it reports
    # itself unconfigured, the analyzer takes the deterministic path, and the
    # investigation says `ai_generated: false` — which is a supported outcome,
    # not an error.
    return "openai"


def build_provider(config=None) -> LLMProvider:
    from app.core.config import settings

    config = config or settings
    name = resolve_name(config)

    if name == "openai":
        return OpenAIProvider(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.llm_base_url or OPENAI_URL,
            timeout=config.llm_timeout_seconds,
        )

    if name == "anthropic":
        return AnthropicProvider(
            api_key=config.anthropic_api_key,
            model=config.anthropic_model,
            base_url=config.llm_base_url or ANTHROPIC_URL,
            timeout=config.llm_timeout_seconds,
            max_tokens=config.llm_max_tokens,
        )

    if name == "compatible":
        return OpenAICompatibleProvider(
            api_key=config.openai_api_key,
            model=config.openai_model,
            base_url=config.llm_base_url,
            timeout=config.llm_timeout_seconds,
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER {name!r}; expected one of {', '.join(PROVIDERS)}. "
        f"Leave it unset to infer from whichever API key is configured."
    )
