"""Mode definitions and configuration for AI Collab collaboration modes."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ModeType(Enum):
    """Available collaboration modes."""
    ROUNDTABLE = "roundtable"
    DEVILS_ADVOCATE = "devils-advocate"
    BRAINSTORM = "brainstorm"
    CHAIN = "chain"
    ROLES = "roles"
    CODE_REVIEW = "code-review"
    SOLVE = "solve"


class TerminationCondition(Enum):
    """How a mode should terminate."""
    ROUNDS = "rounds"           # Fixed number of rounds
    SATISFIED = "satisfied"     # Judge AI rates output >= threshold
    CONSENSUS = "consensus"     # AIs reach agreement


# Built-in role templates
ROLE_TEMPLATES = {
    "architecture": {
        "architect": "Design the system architecture, considering scalability and maintainability",
        "critic": "Challenge the design, identify weaknesses, edge cases, and potential issues",
        "implementer": "Focus on practical implementation details and code structure",
    },
    "debate": {
        "advocate": "Argue in favor of the proposal, present the strongest case",
        "opponent": "Argue against the proposal, present counterarguments and risks",
        "moderator": "Synthesize both perspectives and provide a balanced verdict",
    },
    "review": {
        "author": "Explain the intent and context of the code or content",
        "reviewer": "Critique the work, suggest improvements and identify issues",
        "editor": "Refine and polish based on the review feedback",
    },
    "research": {
        "researcher": "Explore the topic deeply, gather relevant information",
        "skeptic": "Question claims, ask for evidence, identify gaps in reasoning",
        "synthesizer": "Combine findings into a coherent, actionable summary",
    },
}


@dataclass
class ModeConfig:
    """Configuration for a collaboration mode execution."""
    mode_type: ModeType

    # Round/iteration control
    rounds: int = 3
    max_rounds: int = 10

    # Termination
    until: Optional[TerminationCondition] = None
    satisfaction_threshold: int = 8  # For --until=satisfied (1-10 scale)

    # Decision making
    judge: Optional[str] = None      # AI to use as judge
    vote: bool = False               # Enable voting mode

    # Control flow
    checkpoint: bool = False         # Pause after each round
    parallel: bool = False           # Run queries in parallel where applicable

    # AI selection
    order: Optional[list[str]] = None  # Custom AI order for chain/roundtable

    # Roles
    roles: dict[str, str] = field(default_factory=dict)  # role_name -> ai_name
    template: Optional[str] = None    # Role template name

    # Implementation
    implement: bool = False           # Whether to write code at end
    implementer: Optional[str] = None # Which AI implements (default: last)

    def get_effective_roles(self) -> dict[str, str]:
        """Get roles with template applied and overrides merged."""
        if self.template and self.template in ROLE_TEMPLATES:
            # Start with template
            result = {role: "" for role in ROLE_TEMPLATES[self.template].keys()}
            # Apply overrides
            result.update(self.roles)
            return result
        return self.roles

    def get_role_prompt(self, role: str) -> str:
        """Get the prompt instruction for a role."""
        if self.template and self.template in ROLE_TEMPLATES:
            return ROLE_TEMPLATES[self.template].get(role, "")
        return ""


# Default configurations per mode
MODE_DEFAULTS = {
    ModeType.ROUNDTABLE: ModeConfig(
        mode_type=ModeType.ROUNDTABLE,
        rounds=1,  # 1 round = each AI speaks once
    ),
    ModeType.DEVILS_ADVOCATE: ModeConfig(
        mode_type=ModeType.DEVILS_ADVOCATE,
        rounds=2,  # Point and counterpoint exchanges
    ),
    ModeType.BRAINSTORM: ModeConfig(
        mode_type=ModeType.BRAINSTORM,
        rounds=1,
        parallel=True,  # Ideas generated in parallel
    ),
    ModeType.CHAIN: ModeConfig(
        mode_type=ModeType.CHAIN,
        rounds=3,  # A -> B -> C refinement
    ),
    ModeType.ROLES: ModeConfig(
        mode_type=ModeType.ROLES,
        rounds=1,  # Each role speaks once
    ),
    ModeType.CODE_REVIEW: ModeConfig(
        mode_type=ModeType.CODE_REVIEW,
        rounds=1,
        parallel=True,  # Reviews in parallel
    ),
    ModeType.SOLVE: ModeConfig(
        mode_type=ModeType.SOLVE,
        rounds=3,  # Understand -> Plan -> Implement
    ),
}


def get_default_config(mode_type: ModeType) -> ModeConfig:
    """Get default configuration for a mode type."""
    if mode_type in MODE_DEFAULTS:
        # Return a copy to avoid mutation
        default = MODE_DEFAULTS[mode_type]
        return ModeConfig(
            mode_type=default.mode_type,
            rounds=default.rounds,
            max_rounds=default.max_rounds,
            parallel=default.parallel,
        )
    return ModeConfig(mode_type=mode_type)


def parse_mode_name(name: str) -> Optional[ModeType]:
    """Parse a mode name string to ModeType enum."""
    name_lower = name.lower().replace("_", "-")
    for mode in ModeType:
        if mode.value == name_lower:
            return mode
    return None
