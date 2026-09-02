"""A provider that never calls a network.

Useful for demos, tests, and running the deterministic half of the app with no
credentials at all. It returns schema-shaped stubs so downstream code that
expects JSON keeps working.
"""

from __future__ import annotations

from typing import Any

from .base import LLMConfig, LLMProvider


class OfflineProvider(LLMProvider):
    name = "offline"

    def __init__(self, config: LLMConfig | None = None) -> None:
        super().__init__(config or LLMConfig(provider="offline", model="offline-stub"))

    def complete(
        self,
        prompt: str,
        system: str = "",
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if json_schema:
            import json

            return json.dumps(_stub(json_schema))
        return "AI analysis is disabled (offline provider selected in Settings)."

    def health_check(self) -> tuple[bool, str]:
        return True, "offline stub — deterministic scoring only"


def _stub(schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind == "object":
        return {k: _stub(v) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind == "number" or kind == "integer":
        return 0
    if kind == "boolean":
        return False
    return "offline"
