"""Anthropic provider, using the official `anthropic` SDK."""

from __future__ import annotations

import os
from typing import Any

from .base import LLMConfig, LLMError, LLMProvider

# Kept in one place so the Settings dropdown and the default stay in sync.
ANTHROPIC_MODELS = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
]


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency hint
            raise LLMError("The `anthropic` package is not installed. `pip install anthropic`") from exc

        self._anthropic = anthropic
        kwargs: dict[str, Any] = {"timeout": float(config.timeout)}
        key = config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            kwargs["api_key"] = key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        # With no key set, the SDK falls back to an `ant auth login` profile.
        self.client = anthropic.Anthropic(**kwargs)

    def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        request: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            request["system"] = system
        if json_schema:
            # Structured outputs: the first text block is guaranteed valid JSON.
            request["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
        # Effort is a cheap quality/cost dial; medium suits per-posting scoring.
        request.setdefault("output_config", {})
        request["output_config"]["effort"] = self.config.extra.get("effort", "medium")

        try:
            response = self.client.messages.create(**request)
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise LLMError(f"Model declined this request ({getattr(detail, 'category', 'unknown')}).")
        return "".join(b.text for b in response.content if b.type == "text")
