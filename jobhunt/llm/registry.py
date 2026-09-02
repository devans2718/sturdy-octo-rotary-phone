"""Provider lookup + config <-> dict helpers used by the Settings page."""

from __future__ import annotations

from typing import Any

from .anthropic_provider import ANTHROPIC_MODELS, AnthropicProvider
from .base import LLMConfig, LLMError, LLMProvider
from .offline import OfflineProvider
from .openai_compatible import OpenAICompatibleProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "offline": OfflineProvider,
}

# Presets shown in the UI. `base_url` "" means "provider default".
PRESETS: dict[str, dict[str, Any]] = {
    "Anthropic API": {"provider": "anthropic", "model": "claude-opus-5", "base_url": ""},
    "OpenAI API": {
        "provider": "openai-compatible",
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    "Ollama (local)": {
        "provider": "openai-compatible",
        "model": "llama3.1",
        "base_url": "http://localhost:11434/v1",
    },
    "LM Studio (local)": {
        "provider": "openai-compatible",
        "model": "local-model",
        "base_url": "http://localhost:1234/v1",
    },
    "vLLM / custom endpoint": {
        "provider": "openai-compatible",
        "model": "your-model",
        "base_url": "http://localhost:8000/v1",
    },
    "Offline (no AI)": {"provider": "offline", "model": "offline-stub", "base_url": ""},
}

SETTINGS_KEY = "llm_config"


def build_provider(config: LLMConfig) -> LLMProvider:
    cls = PROVIDERS.get(config.provider)
    if cls is None:
        raise LLMError(f"Unknown provider {config.provider!r}. Known: {', '.join(PROVIDERS)}")
    return cls(config)


def config_from_dict(data: dict[str, Any] | None) -> LLMConfig:
    data = dict(data or {})
    known = {f for f in LLMConfig.__dataclass_fields__}
    extra = data.pop("extra", {}) or {}
    return LLMConfig(**{k: v for k, v in data.items() if k in known}, extra=extra)


def config_to_dict(config: LLMConfig) -> dict[str, Any]:
    return dict(config.__dict__)


__all__ = [
    "ANTHROPIC_MODELS",
    "PRESETS",
    "PROVIDERS",
    "SETTINGS_KEY",
    "LLMConfig",
    "LLMError",
    "LLMProvider",
    "build_provider",
    "config_from_dict",
    "config_to_dict",
]
