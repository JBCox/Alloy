"""Tooltip system for Alloy GUI."""

import tkinter as tk
from typing import Optional

from .styles import COLORS, FONTS, PAD


class ToolTip:
    """
    Hover tooltip for any Tkinter widget.

    Usage:
        button = ttk.Button(parent, text="Click me")
        ToolTip(button, "This button does something useful")
    """

    def __init__(self, widget: tk.Widget, text: str, delay: int = 500):
        """
        Create a tooltip for a widget.

        Args:
            widget: The widget to attach the tooltip to
            text: The tooltip text to display
            delay: Milliseconds to wait before showing (default: 500ms)
        """
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tooltip_window: Optional[tk.Toplevel] = None
        self.scheduled_id: Optional[str] = None

        # Bind events
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, event):
        """Schedule tooltip display on mouse enter."""
        self._cancel_scheduled()
        self.scheduled_id = self.widget.after(self.delay, self._show_tooltip)

    def _on_leave(self, event):
        """Cancel and hide tooltip on mouse leave."""
        self._cancel_scheduled()
        self._hide_tooltip()

    def _cancel_scheduled(self):
        """Cancel any scheduled tooltip display."""
        if self.scheduled_id:
            self.widget.after_cancel(self.scheduled_id)
            self.scheduled_id = None

    def _show_tooltip(self):
        """Display the tooltip window."""
        if self.tooltip_window:
            return

        # Get widget position
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5

        # Create tooltip window
        self.tooltip_window = tk.Toplevel(self.widget)
        self.tooltip_window.wm_overrideredirect(True)  # No window decorations
        self.tooltip_window.wm_geometry(f"+{x}+{y}")

        # Tooltip styling
        frame = tk.Frame(
            self.tooltip_window,
            background=COLORS["bg_light"],
            borderwidth=1,
            relief="solid"
        )
        frame.pack()

        label = tk.Label(
            frame,
            text=self.text,
            background=COLORS["bg_light"],
            foreground=COLORS["fg"],
            font=FONTS["small"],
            padx=PAD["small"],
            pady=PAD["xs"],
            wraplength=300,
            justify="left"
        )
        label.pack()

        # Ensure tooltip doesn't go off-screen
        self.tooltip_window.update_idletasks()
        screen_width = self.widget.winfo_screenwidth()
        screen_height = self.widget.winfo_screenheight()
        tooltip_width = self.tooltip_window.winfo_width()
        tooltip_height = self.tooltip_window.winfo_height()

        if x + tooltip_width > screen_width:
            x = screen_width - tooltip_width - 5
        if y + tooltip_height > screen_height:
            y = self.widget.winfo_rooty() - tooltip_height - 5

        self.tooltip_window.wm_geometry(f"+{x}+{y}")

    def _hide_tooltip(self):
        """Hide and destroy the tooltip window."""
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

    def update_text(self, new_text: str):
        """Update the tooltip text."""
        self.text = new_text

    def destroy(self):
        """Clean up tooltip bindings."""
        self._cancel_scheduled()
        self._hide_tooltip()
        try:
            self.widget.unbind("<Enter>")
            self.widget.unbind("<Leave>")
            self.widget.unbind("<ButtonPress>")
        except tk.TclError:
            pass  # Widget already destroyed


def add_tooltip(widget: tk.Widget, text: str, delay: int = 500) -> ToolTip:
    """
    Convenience function to add a tooltip to a widget.

    Args:
        widget: The widget to attach the tooltip to
        text: The tooltip text to display
        delay: Milliseconds to wait before showing

    Returns:
        The created ToolTip instance
    """
    return ToolTip(widget, text, delay)


# Common tooltip texts for reuse
TOOLTIPS = {
    # Display settings
    "show_timestamps": "Show the time each message was sent",
    "show_ai_header": "Display the AI name above each response",
    "max_width": "Maximum character width for responses (0 = no limit)",
    "refresh_rate": "How often the display updates during streaming (updates per second)",
    "preview_length": "Number of characters to show in history preview",
    "verbose_mode": "Output verbosity: silent (errors only), normal, verbose (detailed), debug (all)",

    # Execution settings
    "streaming": "Show AI responses as they generate, character by character",
    "parallel": "Run multiple AI queries at the same time instead of one after another",
    "all_parallel": "Execute @all queries to all AIs simultaneously",
    "timeout": "Maximum seconds to wait for an AI response (0 = no limit)",
    "max_concurrent": "Maximum number of AIs to query at once during parallel execution",
    "max_concurrent_ais": "Maximum number of AIs to query at once during parallel execution",
    "retry_count": "Number of times to retry if an AI query fails",
    "retry_delay": "Seconds to wait between retry attempts",

    # Context settings
    "max_history": "Maximum number of messages to keep in conversation history",
    "auto_context": "Automatically include the last N messages in new queries (0 = disabled)",
    "save_conversations": "Automatically save conversations to disk",
    "save_path": "Directory where conversations will be saved",

    # Mode settings
    "mode_rounds": "Default number of discussion rounds for this mode",
    "mode_parallel": "Run this mode's AI queries in parallel when possible",
    "satisfaction_threshold": "Quality score (1-10) required to consider a solution satisfactory",
    "max_rounds": "Maximum rounds before forcing completion (safety limit)",
    "voting_system": "How consensus is determined: majority, unanimous, or weighted by AI priority",

    # AI settings
    "ai_enabled": "Enable or disable this AI for use in queries",
    "ai_command": "Command to execute this AI CLI. Use {message} as placeholder for the query",
    "ai_color": "Color used to identify this AI's responses in the terminal",
    "ai_description": "Short description of this AI shown in help text",
    "ai_timeout": "Per-AI timeout override (0 = use global timeout)",
    "ai_retry": "Per-AI retry count override",
    "ai_weight": "Priority weight for voting (higher = more influence)",
    "ai_fallback": "Backup AI to use if this one fails",

    # Templates
    "template_name": "Unique name for this role template",
    "template_roles": "Define roles as 'role_name: description', one per line",

    # Advanced
    "config_path": "Path to the configuration file",
    "reset_defaults": "Reset all settings to their default values",
    "export_config": "Export current configuration to a file",
    "import_config": "Import configuration from a file",
}
