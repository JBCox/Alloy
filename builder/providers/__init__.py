"""Provider adapters: real CLI adapters (Phase 2a) and scripted mocks. Never a fallback between them (D3, R-20)."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..config import AgentBinding
    from .base import ProviderAdapter

SUPPORTED_PROVIDERS = ("claude", "gemini", "codex")


def make_adapter(binding: "AgentBinding") -> "ProviderAdapter":
    """Build the adapter a config binding names. Unknown providers are refused, never substituted (R-20)."""
    provider = (binding.provider or "").strip().lower()
    if provider == "claude":
        from .claude import ClaudeAdapter

        return ClaudeAdapter(executable=binding.executable)
    if provider == "gemini":
        from .gemini import GeminiAdapter

        return GeminiAdapter(executable=binding.executable)
    if provider == "codex":
        from .codex import CodexAdapter

        return CodexAdapter(executable=binding.executable)
    raise ValueError(f"agent {binding.label}: provider {binding.provider!r} has no builder adapter; supported: "
                     f"{', '.join(SUPPORTED_PROVIDERS)} (or --mock <screenplay.json> for scripted agents)")
