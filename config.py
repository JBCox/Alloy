"""Configuration handling for AI Collab."""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import yaml


@dataclass
class AIConfig:
    """Configuration for a single AI CLI."""
    name: str
    command: str
    color: str = "white"
    enabled: bool = True
    description: str = ""


@dataclass
class Config:
    """Main configuration."""
    default_ai: str = "claude"
    ais: dict[str, AIConfig] = field(default_factory=dict)
    debate_rounds: int = 3
    all_parallel: bool = False
    show_timestamps: bool = True
    max_width: int = 120
    show_ai_header: bool = True
    max_history: int = 50
    auto_context: int = 0

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load configuration from YAML file."""
        if config_path is None:
            # Look for config in current dir, then home dir
            candidates = [
                Path.cwd() / "config.yaml",
                Path.home() / "ai-collab" / "config.yaml",
                Path.home() / ".config" / "ai-collab" / "config.yaml",
            ]
            for candidate in candidates:
                if candidate.exists():
                    config_path = candidate
                    break

        if config_path is None or not config_path.exists():
            # Return default config
            return cls._default_config()

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls._from_dict(data)

    @classmethod
    def _default_config(cls) -> "Config":
        """Return sensible defaults."""
        return cls(
            default_ai="claude",
            ais={
                "claude": AIConfig(
                    name="claude",
                    command="claude -p {message}",
                    color="cyan",
                    description="Anthropic's Claude Code CLI"
                ),
                "gemini": AIConfig(
                    name="gemini",
                    command="gemini -p {message}",
                    color="blue",
                    description="Google's Gemini CLI"
                ),
                "copilot": AIConfig(
                    name="copilot",
                    command="copilot -p {message}",
                    color="green",
                    description="GitHub Copilot CLI"
                ),
            }
        )

    @classmethod
    def _from_dict(cls, data: dict) -> "Config":
        """Parse config from dictionary."""
        ais = {}
        for name, ai_data in data.get("ais", {}).items():
            if isinstance(ai_data, dict):
                ais[name] = AIConfig(
                    name=name,
                    command=ai_data.get("command", ""),
                    color=ai_data.get("color", "white"),
                    enabled=ai_data.get("enabled", True),
                    description=ai_data.get("description", ""),
                )

        special = data.get("special_commands", {})
        display = data.get("display", {})
        context = data.get("context", {})

        return cls(
            default_ai=data.get("default_ai", "claude"),
            ais=ais,
            debate_rounds=special.get("debate_rounds", 3),
            all_parallel=special.get("all_parallel", False),
            show_timestamps=display.get("show_timestamps", True),
            max_width=display.get("max_width", 120),
            show_ai_header=display.get("show_ai_header", True),
            max_history=context.get("max_history", 50),
            auto_context=context.get("auto_context", 0),
        )

    def get_enabled_ais(self) -> dict[str, AIConfig]:
        """Get only enabled AIs."""
        return {name: ai for name, ai in self.ais.items() if ai.enabled}

    def get_ai(self, name: str) -> Optional[AIConfig]:
        """Get AI config by name (case-insensitive)."""
        name_lower = name.lower()
        for ai_name, ai in self.ais.items():
            if ai_name.lower() == name_lower and ai.enabled:
                return ai
        return None
