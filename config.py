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
    # Advanced per-AI settings
    timeout: int = 0           # Per-AI timeout override (0 = use global)
    retry_count: int = 0       # Per-AI retry count
    weight: int = 1            # Priority weight for voting (higher = more influence)
    fallback_ai: str = ""      # Backup AI to use if this one fails


@dataclass
class ModeSettings:
    """Settings for a specific collaboration mode."""
    rounds: int = 1
    parallel: bool = False
    checkpoint: bool = False


@dataclass
class RoleTemplate:
    """Custom role template definition."""
    name: str
    roles: dict[str, str] = field(default_factory=dict)  # role_name -> description


@dataclass
class ModeGlobalSettings:
    """Global settings that apply to all collaboration modes."""
    satisfaction_threshold: int = 8    # Quality score (1-10) for --until=satisfied
    max_rounds: int = 10               # Safety cap on all modes
    voting_system: str = "majority"    # "majority", "unanimous", or "weighted"


@dataclass
class Config:
    """Main configuration."""
    # AI selection
    default_ai: str = "claude"
    default_judge: str = ""  # AI to use for judging (empty = use default_ai)
    ais: dict[str, AIConfig] = field(default_factory=dict)

    # Legacy/special commands
    debate_rounds: int = 3
    all_parallel: bool = False

    # Display settings
    show_timestamps: bool = True
    max_width: int = 120
    show_ai_header: bool = True
    verbose_mode: str = "normal"   # "silent", "normal", "verbose", "debug"
    preview_length: int = 100      # History preview truncation length

    # Context settings
    max_history: int = 50
    auto_context: int = 0
    save_conversations: bool = False  # Auto-save sessions
    save_path: str = ""               # Conversation save directory

    # Execution settings
    streaming: bool = True            # Stream output as it arrives
    parallel: bool = False            # Parallel execution (opt-in)
    refresh_rate: int = 10            # Rich Live refresh rate (updates per second)
    timeout: int = 300                # Global timeout in seconds
    max_concurrent_ais: int = 3       # Limit parallel queries
    retry_count: int = 0              # Retry failed queries
    retry_delay: int = 2              # Seconds between retries

    # Mode-specific settings
    mode_settings: dict[str, ModeSettings] = field(default_factory=dict)
    mode_global: ModeGlobalSettings = field(default_factory=ModeGlobalSettings)

    # Custom role templates
    custom_templates: dict[str, RoleTemplate] = field(default_factory=dict)

    @classmethod
    def config_exists(cls) -> bool:
        """Check if a config file exists in any standard location."""
        candidates = [
            Path.cwd() / "config.yaml",
            Path.home() / "Alloy" / "config.yaml",
            Path.home() / ".config" / "alloy" / "config.yaml",
        ]
        return any(c.exists() for c in candidates)

    @classmethod
    def get_config_path(cls) -> Path:
        """Get the path to the config file (existing or default location)."""
        candidates = [
            Path.cwd() / "config.yaml",
            Path.home() / "Alloy" / "config.yaml",
            Path.home() / ".config" / "alloy" / "config.yaml",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        # Return default location
        return Path.cwd() / "config.yaml"

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load configuration from YAML file."""
        if config_path is None:
            # Look for config in current dir, then home dir
            candidates = [
                Path.cwd() / "config.yaml",
                Path.home() / "Alloy" / "config.yaml",
                Path.home() / ".config" / "alloy" / "config.yaml",
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
        # Parse AI configurations
        ais = {}
        for name, ai_data in data.get("ais", {}).items():
            if isinstance(ai_data, dict):
                ais[name] = AIConfig(
                    name=name,
                    command=ai_data.get("command", ""),
                    color=ai_data.get("color", "white"),
                    enabled=ai_data.get("enabled", True),
                    description=ai_data.get("description", ""),
                    timeout=ai_data.get("timeout", 0),
                    retry_count=ai_data.get("retry_count", 0),
                    weight=ai_data.get("weight", 1),
                    fallback_ai=ai_data.get("fallback_ai", ""),
                )

        special = data.get("special_commands", {})
        display = data.get("display", {})
        context = data.get("context", {})
        execution = data.get("execution", {})

        # Parse mode settings
        mode_settings = {}
        modes_data = data.get("modes", {})
        for mode_name, mode_data in modes_data.items():
            if mode_name == "global":
                continue  # Skip global settings here
            if isinstance(mode_data, dict):
                mode_settings[mode_name] = ModeSettings(
                    rounds=mode_data.get("rounds", 1),
                    parallel=mode_data.get("parallel", False),
                    checkpoint=mode_data.get("checkpoint", False),
                )

        # Parse mode global settings
        global_mode_data = modes_data.get("global", {})
        mode_global = ModeGlobalSettings(
            satisfaction_threshold=global_mode_data.get("satisfaction_threshold", 8),
            max_rounds=global_mode_data.get("max_rounds", 10),
            voting_system=global_mode_data.get("voting_system", "majority"),
        )

        # Parse custom templates
        custom_templates = {}
        for tmpl_name, tmpl_data in data.get("custom_templates", {}).items():
            if isinstance(tmpl_data, dict):
                custom_templates[tmpl_name] = RoleTemplate(
                    name=tmpl_name,
                    roles=tmpl_data.get("roles", {}),
                )

        return cls(
            default_ai=data.get("default_ai", "claude"),
            default_judge=data.get("default_judge", ""),
            ais=ais,
            debate_rounds=special.get("debate_rounds", 3),
            all_parallel=special.get("all_parallel", False),
            # Display settings
            show_timestamps=display.get("show_timestamps", True),
            max_width=display.get("max_width", 120),
            show_ai_header=display.get("show_ai_header", True),
            verbose_mode=display.get("verbose_mode", "normal"),
            preview_length=display.get("preview_length", 100),
            # Context settings
            max_history=context.get("max_history", 50),
            auto_context=context.get("auto_context", 0),
            save_conversations=context.get("save_conversations", False),
            save_path=context.get("save_path", ""),
            # Execution settings
            streaming=execution.get("streaming", True),
            parallel=execution.get("parallel", False),
            refresh_rate=execution.get("refresh_rate", 10),
            timeout=execution.get("timeout", 300),
            max_concurrent_ais=execution.get("max_concurrent_ais", 3),
            retry_count=execution.get("retry_count", 0),
            retry_delay=execution.get("retry_delay", 2),
            # Mode settings
            mode_settings=mode_settings,
            mode_global=mode_global,
            custom_templates=custom_templates,
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

    def save(self, config_path: Optional[Path] = None):
        """Save configuration to YAML file."""
        if config_path is None:
            config_path = self.get_config_path()

        # Build modes dict including global settings
        modes = {
            name: {
                "rounds": settings.rounds,
                "parallel": settings.parallel,
                "checkpoint": settings.checkpoint,
            }
            for name, settings in self.mode_settings.items()
        }
        modes["global"] = {
            "satisfaction_threshold": self.mode_global.satisfaction_threshold,
            "max_rounds": self.mode_global.max_rounds,
            "voting_system": self.mode_global.voting_system,
        }

        data = {
            "default_ai": self.default_ai,
            "default_judge": self.default_judge,
            "ais": {
                name: {
                    "command": ai.command,
                    "color": ai.color,
                    "enabled": ai.enabled,
                    "description": ai.description,
                    "timeout": ai.timeout,
                    "retry_count": ai.retry_count,
                    "weight": ai.weight,
                    "fallback_ai": ai.fallback_ai,
                }
                for name, ai in self.ais.items()
            },
            "modes": modes,
            "custom_templates": {
                name: {
                    "roles": tmpl.roles,
                }
                for name, tmpl in self.custom_templates.items()
            },
            "special_commands": {
                "debate_rounds": self.debate_rounds,
                "all_parallel": self.all_parallel,
            },
            "display": {
                "show_timestamps": self.show_timestamps,
                "max_width": self.max_width,
                "show_ai_header": self.show_ai_header,
                "verbose_mode": self.verbose_mode,
                "preview_length": self.preview_length,
            },
            "context": {
                "max_history": self.max_history,
                "auto_context": self.auto_context,
                "save_conversations": self.save_conversations,
                "save_path": self.save_path,
            },
            "execution": {
                "streaming": self.streaming,
                "parallel": self.parallel,
                "refresh_rate": self.refresh_rate,
                "timeout": self.timeout,
                "max_concurrent_ais": self.max_concurrent_ais,
                "retry_count": self.retry_count,
                "retry_delay": self.retry_delay,
            },
        }

        # Ensure directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
