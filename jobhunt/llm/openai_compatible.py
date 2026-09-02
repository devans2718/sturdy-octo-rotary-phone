"""Any endpoint that speaks the OpenAI /chat/completions shape.

Covers OpenAI itself, Ollama (`/v1`), LM Studio, vLLM, llama.cpp's server,
OpenRouter, Together, Groq, and most self-hosted gateways. Uses plain HTTP so
no vendor SDK is required.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from .base import LLMConfig, LLMError, LLMProvider


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self.base_url = (config.base_url or os.environ.get("LOCAL_LLM_BASE_URL") or self.default_base_url).rstrip("/")
        self.api_key = (
            config.api_key
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("LOCAL_LLM_API_KEY")
            or "not-needed"
        )

    def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if json_schema:
            # Many local servers only implement the loose `json_object` mode, so
            # the schema also goes in the prompt (see agent.py) as a fallback.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": json_schema},
            }

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload),
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(f"Could not reach {self.base_url}: {exc}") from exc

        if resp.status_code == 400 and json_schema:
            # Retry without strict schema for servers that reject json_schema.
            payload["response_format"] = {"type": "json_object"}
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=self.config.timeout,
            )
        if resp.status_code >= 400:
            raise LLMError(f"{self.base_url} returned {resp.status_code}: {resp.text[:400]}")

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected response shape from {self.base_url}: {resp.text[:300]}") from exc
