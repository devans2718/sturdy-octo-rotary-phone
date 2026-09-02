"""Provider-agnostic LLM interface.

Everything above this layer (scoring, extraction, advice) talks only to
`LLMProvider`. Swapping Anthropic for a local Ollama model is a config change,
never a code change.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """Any provider failure, normalized so the UI can show one message."""


@dataclass
class LLMConfig:
    provider: str = "anthropic"  # see llm/registry.py PROVIDERS
    model: str = "claude-opus-5"
    api_key: str = ""
    base_url: str = ""  # only meaningful for openai-compatible endpoints
    max_tokens: int = 4000
    temperature: float | None = None
    timeout: int = 120
    extra: dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["api_key"] = f"…{self.api_key[-4:]}" if self.api_key else ""
        return d


class LLMProvider(ABC):
    """Minimal surface: one blocking completion call, optionally JSON-shaped."""

    name = "base"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @abstractmethod
    def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return the model's text response."""

    def complete_json(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Completion parsed as JSON, tolerant of chatty or fenced models."""
        text = self.complete(prompt, system=system, json_schema=json_schema, max_tokens=max_tokens)
        return extract_json(text)

    def health_check(self) -> tuple[bool, str]:
        try:
            reply = self.complete("Reply with the single word: ok", max_tokens=16)
            return True, (reply or "").strip()[:200]
        except Exception as exc:  # surfaced verbatim in Settings
            return False, str(exc)[:500]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Best-effort JSON recovery.

    Structured-output modes make this unnecessary; small local models make it
    essential. Tries: whole string -> fenced block -> first balanced {...}/[...].
    """
    if text is None:
        raise LLMError("empty model response")
    text = text.strip()
    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise LLMError(f"model did not return JSON. First 300 chars: {text[:300]!r}")


def _json_candidates(text: str):
    yield text
    for match in _FENCE.findall(text):
        yield match.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break
