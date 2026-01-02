"""Display interface abstraction for Alloy.

This module defines the abstract interface that both CLI and GUI
display implementations must follow. This allows the core application
logic to be display-agnostic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generator, Optional, Callable, Any
from enum import Enum


class MessageRole(Enum):
    """Role of a message in the conversation."""
    USER = "user"
    AI = "ai"
    SYSTEM = "system"
    ERROR = "error"


@dataclass
class DisplayMessage:
    """Platform-agnostic message representation."""
    role: MessageRole
    content: str
    ai_name: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    is_streaming: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class StreamUpdate:
    """Update during a streaming response."""
    ai_name: str
    content: str  # Full content so far
    chunk: str    # Just the new chunk
    is_complete: bool = False


class DisplayInterface(ABC):
    """Abstract interface for display output.

    Both CLI (Rich) and GUI (Tkinter) implementations must implement
    this interface to receive display events from the application.
    """

    @abstractmethod
    def show_message(self, message: DisplayMessage) -> None:
        """Display a complete message.

        Args:
            message: The message to display
        """
        pass

    @abstractmethod
    def show_streaming_start(self, ai_name: str) -> None:
        """Called when a streaming response begins.

        Args:
            ai_name: Name of the AI starting to respond
        """
        pass

    @abstractmethod
    def show_streaming_chunk(self, ai_name: str, chunk: str, full_content: str) -> None:
        """Called for each chunk of a streaming response.

        Args:
            ai_name: Name of the responding AI
            chunk: The new text chunk
            full_content: Complete content so far
        """
        pass

    @abstractmethod
    def show_streaming_end(self, ai_name: str, full_content: str) -> None:
        """Called when a streaming response completes.

        Args:
            ai_name: Name of the AI that finished
            full_content: The complete response content
        """
        pass

    @abstractmethod
    def show_error(self, message: str, title: str = "Error") -> None:
        """Display an error message.

        Args:
            message: Error description
            title: Error title/category
        """
        pass

    @abstractmethod
    def show_warning(self, message: str) -> None:
        """Display a warning message.

        Args:
            message: Warning text
        """
        pass

    @abstractmethod
    def show_success(self, message: str) -> None:
        """Display a success message.

        Args:
            message: Success text
        """
        pass

    @abstractmethod
    def show_info(self, message: str) -> None:
        """Display an informational message.

        Args:
            message: Info text
        """
        pass

    @abstractmethod
    def show_status(self, message: str) -> None:
        """Update status bar or show temporary status.

        Args:
            message: Status text
        """
        pass

    @abstractmethod
    def show_thinking(self, ai_name: str) -> Any:
        """Show a 'thinking' or loading indicator.

        Args:
            ai_name: Name of the AI that's processing

        Returns:
            A context manager or handle that can be used to stop the indicator
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear the display/conversation view."""
        pass

    @abstractmethod
    def show_ai_list(self, ais: list[dict]) -> None:
        """Display list of configured AIs.

        Args:
            ais: List of AI info dicts with keys: name, enabled, color, description
        """
        pass

    @abstractmethod
    def show_mode_list(self, modes: list[dict]) -> None:
        """Display list of available modes.

        Args:
            modes: List of mode info dicts with keys: name, description
        """
        pass

    @abstractmethod
    def show_history(self, messages: list[dict]) -> None:
        """Display conversation history.

        Args:
            messages: List of history entries with keys: role, content, timestamp
        """
        pass

    @abstractmethod
    def show_help(self, commands: list[tuple[str, str]],
                  modes: list[tuple[str, str]],
                  flags: list[tuple[str, str]]) -> None:
        """Display help information.

        Args:
            commands: List of (command, description) tuples
            modes: List of (mode, description) tuples
            flags: List of (flag, description) tuples
        """
        pass

    @abstractmethod
    def prompt_checkpoint(self, round_num: int, total_rounds: int,
                          responses: list[dict]) -> bool:
        """Prompt user at a checkpoint during mode execution.

        Args:
            round_num: Current round number
            total_rounds: Total expected rounds
            responses: Responses from this round

        Returns:
            True to continue, False to stop
        """
        pass

    @abstractmethod
    def get_user_input(self, prompt: str = "> ") -> Optional[str]:
        """Get input from the user.

        For CLI, this is blocking. For GUI, this might be async/callback-based.

        Args:
            prompt: The prompt to display

        Returns:
            User input string, or None if cancelled/EOF
        """
        pass


class DisplayCallback:
    """Callback-based display for async GUI updates.

    Instead of direct method calls, this queues updates
    that the GUI can process on its main thread.
    """

    def __init__(self):
        self.on_message: Optional[Callable[[DisplayMessage], None]] = None
        self.on_streaming_start: Optional[Callable[[str], None]] = None
        self.on_streaming_chunk: Optional[Callable[[str, str, str], None]] = None
        self.on_streaming_end: Optional[Callable[[str, str], None]] = None
        self.on_error: Optional[Callable[[str, str], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None
        self.on_clear: Optional[Callable[[], None]] = None
