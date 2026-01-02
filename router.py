"""Message parsing and routing for AI Collab."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from modes import ModeType, ModeConfig, TerminationCondition, parse_mode_name, get_default_config


class CommandType(Enum):
    """Types of commands/messages."""
    DIRECT = "direct"       # @claude message
    ALL = "all"            # @all message
    DEBATE = "debate"      # @debate topic (legacy)
    MODE = "mode"          # @roundtable, @chain, etc.
    SYSTEM = "system"      # /command
    DEFAULT = "default"    # No prefix, use default AI


@dataclass
class ParsedMessage:
    """Result of parsing a user message."""
    command_type: CommandType
    target_ai: Optional[str] = None  # For DIRECT commands
    content: str = ""
    system_command: Optional[str] = None  # For /commands
    mode_config: Optional[ModeConfig] = None  # For MODE commands


class Router:
    """Parses and routes messages to appropriate AIs."""

    # Pattern for @mode[options] --flags prompt
    # Groups: 1=mode_name, 2=options (optional), 3=rest (flags + prompt)
    MODE_PATTERN = re.compile(
        r"^@([\w-]+)(?:\[([^\]]*)\])?\s*(.*)$",
        re.DOTALL
    )

    # Pattern for system commands
    SYSTEM_PATTERN = re.compile(r"^/(\w+)(?:\s+(.*))?$", re.DOTALL)

    # Flag patterns
    FLAG_PATTERN = re.compile(r"--(\w+)(?:=(\S+))?")

    # All collaboration mode names
    COLLABORATION_MODES = {
        "roundtable", "devils-advocate", "brainstorm",
        "chain", "roles", "code-review", "solve"
    }

    # Legacy special targets (still supported)
    LEGACY_TARGETS = {"all", "debate"}

    def __init__(self, available_ais: list[str], default_ai: str):
        """
        Initialize router.

        Args:
            available_ais: List of available AI names (lowercase)
            default_ai: Default AI to use when no @mention
        """
        self.available_ais = [ai.lower() for ai in available_ais]
        self.default_ai = default_ai.lower()

    def parse(self, message: str) -> ParsedMessage:
        """
        Parse a user message to determine routing.

        Supports:
            /command              - System commands
            @ai message           - Direct to specific AI
            @all message          - Broadcast to all AIs
            @mode[opts] --flags   - Collaboration modes
            message               - Default AI

        Args:
            message: Raw user input

        Returns:
            ParsedMessage with routing information
        """
        message = message.strip()

        # Check for system commands first (/help, /history, etc.)
        system_match = self.SYSTEM_PATTERN.match(message)
        if system_match:
            return ParsedMessage(
                command_type=CommandType.SYSTEM,
                content=system_match.group(2) or "",
                system_command=system_match.group(1).lower()
            )

        # Check for @mentions (modes and direct)
        mode_match = self.MODE_PATTERN.match(message)
        if mode_match:
            target = mode_match.group(1).lower()
            options_str = mode_match.group(2) or ""
            rest = mode_match.group(3).strip()

            # Is this a collaboration mode?
            mode_type = parse_mode_name(target)
            if mode_type:
                return self._parse_mode_command(mode_type, options_str, rest)

            # Legacy @all
            if target == "all":
                return ParsedMessage(
                    command_type=CommandType.ALL,
                    content=rest
                )

            # Legacy @debate -> convert to @roundtable
            if target == "debate":
                mode_config = get_default_config(ModeType.ROUNDTABLE)
                return ParsedMessage(
                    command_type=CommandType.MODE,
                    content=rest,
                    mode_config=mode_config
                )

            # Direct to specific AI
            if target in self.available_ais:
                return ParsedMessage(
                    command_type=CommandType.DIRECT,
                    target_ai=target,
                    content=rest
                )

            # Unknown target, treat as default with full message
            return ParsedMessage(
                command_type=CommandType.DEFAULT,
                target_ai=self.default_ai,
                content=message
            )

        # No special prefix, use default AI
        return ParsedMessage(
            command_type=CommandType.DEFAULT,
            target_ai=self.default_ai,
            content=message
        )

    def _parse_mode_command(self, mode_type: ModeType, options_str: str, rest: str) -> ParsedMessage:
        """Parse a collaboration mode command."""
        # Start with default config for this mode
        config = get_default_config(mode_type)

        # Parse bracket options [key=value, ...]
        if options_str:
            self._apply_options(config, options_str)

        # Parse --flags from rest, extract prompt
        prompt = self._parse_flags(config, rest)

        return ParsedMessage(
            command_type=CommandType.MODE,
            content=prompt,
            mode_config=config
        )

    def _apply_options(self, config: ModeConfig, options_str: str):
        """Apply bracket options to config."""
        # Parse key=value pairs
        for part in options_str.split(","):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                self._set_config_value(config, key, value)

    def _parse_flags(self, config: ModeConfig, rest: str) -> str:
        """Parse --flags from rest of command, return remaining prompt."""
        parts = []
        i = 0
        tokens = rest.split()

        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--"):
                # Parse flag
                if "=" in token:
                    flag_name, flag_value = token[2:].split("=", 1)
                else:
                    flag_name = token[2:]
                    flag_value = "true"
                self._set_config_value(config, flag_name, flag_value)
            else:
                # This and everything after is the prompt
                parts = tokens[i:]
                break
            i += 1

        return " ".join(parts)

    def _set_config_value(self, config: ModeConfig, key: str, value: str):
        """Set a config value by key name."""
        key = key.lower().replace("-", "_")

        if key == "rounds":
            config.rounds = int(value)
        elif key == "max":
            config.max_rounds = int(value)
        elif key == "judge":
            config.judge = value
        elif key == "order":
            config.order = [ai.strip() for ai in value.split(",")]
        elif key == "template":
            config.template = value
        elif key == "implement":
            if value.lower() in ("true", "1", "yes"):
                config.implement = True
            else:
                config.implement = True
                config.implementer = value
        elif key == "checkpoint":
            config.checkpoint = value.lower() in ("true", "1", "yes")
        elif key == "vote":
            config.vote = value.lower() in ("true", "1", "yes")
        elif key == "parallel":
            config.parallel = value.lower() in ("true", "1", "yes")
        elif key == "until":
            if value == "satisfied":
                config.until = TerminationCondition.SATISFIED
            elif value == "consensus":
                config.until = TerminationCondition.CONSENSUS
        elif key in self.available_ais or key in ("architect", "critic", "implementer",
                                                    "advocate", "opponent", "moderator",
                                                    "author", "reviewer", "editor",
                                                    "researcher", "skeptic", "synthesizer"):
            # Role assignment: architect=claude, critic=gemini, etc.
            config.roles[key] = value

    def substitute_variables(self, content: str, context: dict[str, str]) -> str:
        """
        Substitute context variables in message content.

        Supported variables:
            {last} - Last AI response
            {ai_name} - Last response from specific AI (e.g., {claude})
            {history} - Full conversation history

        Args:
            content: Message content with potential variables
            context: Dictionary of variable values

        Returns:
            Content with variables substituted
        """
        result = content

        # Replace {last}
        if "{last}" in result and "last" in context:
            result = result.replace("{last}", context["last"])

        # Replace {history}
        if "{history}" in result and "history" in context:
            result = result.replace("{history}", context["history"])

        # Replace AI-specific: {claude}, {gemini}, etc.
        for ai_name in self.available_ais:
            placeholder = "{" + ai_name + "}"
            if placeholder in result and ai_name in context:
                result = result.replace(placeholder, context[ai_name])

        return result
