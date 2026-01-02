"""Setup Wizard for first-run configuration of Alloy."""

import subprocess
import sys
import threading
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
from typing import Callable, Optional

import yaml

from .styles import COLORS, FONTS, PAD, WINDOW_SIZES, apply_dark_theme
from .widgets import (
    AIConfigCard, LabeledCheckbox, LabeledDropdown, LabeledSpinbox,
    ScrollableFrame, StepIndicator, YAMLPreview
)


# Known AI CLI configurations
KNOWN_AIS = {
    "claude": {
        "commands": ["claude --version"],
        "default_command": "claude -p {message}",
        "color": "cyan",
        "description": "Anthropic's Claude Code CLI"
    },
    "gemini": {
        "commands": ["gemini --version"],
        "default_command": "gemini -p {message}",
        "color": "blue",
        "description": "Google's Gemini CLI"
    },
    "codex": {
        "commands": ["codex --version"],
        "default_command": "codex exec {message}",
        "color": "green",
        "description": "OpenAI Codex CLI"
    },
    # Note: GitHub Copilot CLI is typically installed via: gh extension install github/gh-copilot
    # The command syntax is: gh copilot suggest "message" or gh copilot explain "message"
    # It doesn't work the same way as other AI CLIs - it's interactive and context-specific
    # Uncomment and adjust if you have it installed:
    # "copilot": {
    #     "commands": ["gh copilot --version"],
    #     "default_command": "gh copilot suggest {message}",
    #     "color": "magenta",
    #     "description": "GitHub Copilot CLI (via gh extension)"
    # },
}


class SetupWizard(tk.Toplevel):
    """First-run setup wizard for Alloy."""

    def __init__(self, parent=None, on_complete: Callable = None,
                 config_path: Path = None):
        super().__init__(parent)

        self.on_complete = on_complete
        self.config_path = config_path or Path.cwd() / "config.yaml"
        self.detected_ais = {}
        self.ai_cards = {}
        self.current_page = 0
        self.pages = []

        # Window setup
        self.title("Alloy Setup")
        self.geometry(f"{WINDOW_SIZES['wizard'][0]}x{WINDOW_SIZES['wizard'][1]}")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)

        # Center window
        self.update_idletasks()
        x = (self.winfo_screenwidth() - WINDOW_SIZES['wizard'][0]) // 2
        y = (self.winfo_screenheight() - WINDOW_SIZES['wizard'][1]) // 2
        self.geometry(f"+{x}+{y}")

        # Apply theme
        apply_dark_theme(self)

        # Handle close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Create UI
        self._create_ui()

    def _create_ui(self):
        """Create the wizard UI."""
        # Main container
        self.main_frame = ttk.Frame(self)
        self.main_frame.pack(fill="both", expand=True, padx=PAD["large"], pady=PAD["large"])

        # Step indicator at top
        self.step_indicator = StepIndicator(
            self.main_frame,
            total_steps=5,
            step_names=["Welcome", "Detection", "Configure AIs", "Preferences", "Summary"]
        )
        self.step_indicator.pack(pady=(0, PAD["medium"]))

        # Page container
        self.page_container = ttk.Frame(self.main_frame)
        self.page_container.pack(fill="both", expand=True)

        # Create all pages
        self._create_pages()

        # Navigation buttons
        self._create_nav_buttons()

        # Show first page
        self._show_page(0)

    def _create_pages(self):
        """Create all wizard pages."""
        self.pages = [
            self._create_welcome_page(),
            self._create_detection_page(),
            self._create_ai_config_page(),
            self._create_preferences_page(),
            self._create_summary_page(),
        ]

    def _create_nav_buttons(self):
        """Create navigation buttons with keyboard shortcuts."""
        nav_frame = ttk.Frame(self.main_frame)
        nav_frame.pack(fill="x", pady=(PAD["large"], 0))

        self.back_btn = ttk.Button(nav_frame, text="Back", command=self._prev_page)
        self.back_btn.pack(side="left")

        self.next_btn = ttk.Button(nav_frame, text="Next", command=self._next_page,
                                   style="Accent.TButton")
        self.next_btn.pack(side="right")

        # Keyboard shortcuts
        self.bind_all("<Return>", lambda e: self._next_page())
        self.bind_all("<Escape>", lambda e: self._on_close())

    def _show_page(self, index: int):
        """Show a specific page."""
        # Hide all pages
        for page in self.pages:
            page.pack_forget()

        # Show current page
        self.current_page = index
        self.pages[index].pack(fill="both", expand=True)

        # Update step indicator
        self.step_indicator.set_step(index)

        # Update nav buttons
        self.back_btn.configure(state="normal" if index > 0 else "disabled")

        if index == len(self.pages) - 1:
            self.next_btn.configure(text="Finish")
        else:
            self.next_btn.configure(text="Next")

        # Special handling for detection page
        if index == 1 and not self.detected_ais:
            self.after(100, self._run_detection)

    def _next_page(self):
        """Go to next page."""
        if self.current_page == len(self.pages) - 1:
            # Finish button pressed
            self._finish_wizard()
        else:
            # Validate current page
            if self._validate_page():
                self._show_page(self.current_page + 1)
                # Update summary when reaching last page
                if self.current_page == len(self.pages) - 1:
                    self._update_summary()

    def _prev_page(self):
        """Go to previous page."""
        if self.current_page > 0:
            self._show_page(self.current_page - 1)

    def _validate_page(self) -> bool:
        """Validate current page. Returns True if valid."""
        if self.current_page == 2:  # AI config page
            for name, card in self.ai_cards.items():
                error = card.validate()
                if error:
                    self._show_error(error)
                    return False
        return True

    def _show_error(self, message: str):
        """Show error message."""
        tk.messagebox.showerror("Validation Error", message)

    # ==================== PAGE CREATION ====================

    def _create_welcome_page(self) -> ttk.Frame:
        """Create welcome page."""
        page = ttk.Frame(self.page_container)

        # Centered content
        center = ttk.Frame(page)
        center.place(relx=0.5, rely=0.4, anchor="center")

        # Logo/Title
        title = ttk.Label(center, text="Alloy", style="Heading.TLabel")
        title.configure(font=("Segoe UI", 36, "bold"))
        title.pack()

        subtitle = ttk.Label(center, text="Multiple AIs, stronger together",
                            style="Dim.TLabel")
        subtitle.configure(font=FONTS["subheading"])
        subtitle.pack(pady=(PAD["small"], PAD["xl"]))

        # Description
        desc = ttk.Label(
            center,
            text="This wizard will help you set up Alloy by detecting\n"
                 "installed AI CLIs and configuring your preferences.",
            style="TLabel",
            justify="center"
        )
        desc.pack(pady=PAD["large"])

        # Get started hint
        hint = ttk.Label(center, text="Click Next to begin", style="Dim.TLabel")
        hint.pack(pady=PAD["large"])

        return page

    def _create_detection_page(self) -> ttk.Frame:
        """Create AI detection page."""
        page = ttk.Frame(self.page_container)

        # Title
        ttk.Label(page, text="Detecting AI CLIs", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Scanning for installed AI command-line tools...",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["small"], PAD["large"]))

        # Progress bar
        self.detection_progress = ttk.Progressbar(page, mode="indeterminate", length=300)
        self.detection_progress.pack(pady=PAD["medium"])

        # Results frame
        self.detection_results = ttk.Frame(page)
        self.detection_results.pack(fill="both", expand=True, pady=PAD["medium"])

        # Rescan button
        rescan_btn = ttk.Button(page, text="Rescan", command=self._run_detection)
        rescan_btn.pack(anchor="w", pady=PAD["medium"])

        return page

    def _create_ai_config_page(self) -> ttk.Frame:
        """Create AI configuration page."""
        page = ttk.Frame(self.page_container)

        # Title
        ttk.Label(page, text="Configure AIs", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Configure each AI's command and settings. Disable any you don't want to use.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["small"], PAD["large"]))

        # Scrollable frame for AI cards
        self.ai_scroll = ScrollableFrame(page)
        self.ai_scroll.pack(fill="both", expand=True)

        return page

    def _create_preferences_page(self) -> ttk.Frame:
        """Create preferences page."""
        page = ttk.Frame(self.page_container)

        # Title
        ttk.Label(page, text="Preferences", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Configure your default settings.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["small"], PAD["large"]))

        # Settings grid
        settings = ttk.Frame(page)
        settings.pack(fill="x", pady=PAD["medium"])

        # Default AI dropdown
        self.default_ai = LabeledDropdown(
            settings,
            label="Default AI (used when no @mention):",
            options=["claude", "gemini", "codex"]  # Will be updated after detection
        )
        self.default_ai.pack(anchor="w", pady=PAD["small"])

        # Streaming
        self.streaming = LabeledCheckbox(
            settings,
            label="Enable streaming (show output as it generates)",
            default=True
        )
        self.streaming.pack(anchor="w", pady=PAD["small"])

        # Parallel
        self.parallel = LabeledCheckbox(
            settings,
            label="Enable parallel execution for @all queries",
            default=False
        )
        self.parallel.pack(anchor="w", pady=PAD["small"])

        ttk.Separator(settings, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Display settings
        ttk.Label(settings, text="Display Settings", style="Subheading.TLabel").pack(anchor="w")

        self.show_timestamps = LabeledCheckbox(
            settings,
            label="Show timestamps on messages",
            default=True
        )
        self.show_timestamps.pack(anchor="w", pady=PAD["small"])

        self.show_ai_header = LabeledCheckbox(
            settings,
            label="Show AI name in response headers",
            default=True
        )
        self.show_ai_header.pack(anchor="w", pady=PAD["small"])

        self.max_width = LabeledSpinbox(
            settings,
            label="Max response width (0 = no limit):",
            from_=0,
            to=300,
            default=120
        )
        self.max_width.pack(anchor="w", pady=PAD["small"])

        return page

    def _create_summary_page(self) -> ttk.Frame:
        """Create summary page."""
        page = ttk.Frame(self.page_container)

        # Title
        ttk.Label(page, text="Ready to Go!", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Review your configuration below. Click Finish to save and start Alloy.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["small"], PAD["large"]))

        # Config path
        path_frame = ttk.Frame(page)
        path_frame.pack(fill="x", pady=PAD["small"])
        ttk.Label(path_frame, text="Config will be saved to:", style="TLabel").pack(side="left")
        ttk.Label(path_frame, text=str(self.config_path), style="Dim.TLabel").pack(side="left", padx=PAD["small"])

        # YAML preview
        self.yaml_preview = YAMLPreview(page)
        self.yaml_preview.pack(fill="both", expand=True, pady=PAD["medium"])

        return page

    # ==================== DETECTION ====================

    def _run_detection(self):
        """Run AI detection in background thread."""
        self.detection_progress.start()

        # Clear previous results
        for widget in self.detection_results.winfo_children():
            widget.destroy()

        # Run detection in thread
        thread = threading.Thread(target=self._detect_ais)
        thread.start()

    def _detect_ais(self):
        """Detect available AI CLIs (runs in background thread)."""
        results = {}

        for name, info in KNOWN_AIS.items():
            detected = False
            for cmd in info["commands"]:
                try:
                    subprocess.run(
                        cmd,
                        shell=True,
                        capture_output=True,
                        timeout=5
                    )
                    detected = True
                    break
                except (subprocess.SubprocessError, FileNotFoundError):
                    continue

            results[name] = detected

        # Update UI on main thread
        self.after(0, lambda: self._on_detection_complete(results))

    def _on_detection_complete(self, results: dict):
        """Handle detection results (called on main thread)."""
        self.detection_progress.stop()
        self.detected_ais = results

        # Show results
        for name, detected in results.items():
            frame = ttk.Frame(self.detection_results)
            frame.pack(fill="x", pady=PAD["xs"])

            status = "Found" if detected else "Not found"
            status_style = "Success.TLabel" if detected else "Dim.TLabel"
            icon = "\u2713" if detected else "\u2717"

            ttk.Label(frame, text=icon, style=status_style).pack(side="left")
            ttk.Label(frame, text=f"  {name.capitalize()}", style="TLabel").pack(side="left")
            ttk.Label(frame, text=f"  ({status})", style=status_style).pack(side="left")

        # Update AI config page
        self._update_ai_cards()

        # Update default AI dropdown
        detected_list = [name for name, d in results.items() if d]
        if detected_list:
            self.default_ai.set_options(detected_list)
            self.default_ai.set(detected_list[0])
        else:
            # No AIs detected - show fallback option
            self.default_ai.set_options(["(none detected)"])
            self.default_ai.set("(none detected)")

    def _update_ai_cards(self):
        """Update AI configuration cards based on detection."""
        # Clear existing cards
        for widget in self.ai_scroll.scrollable_frame.winfo_children():
            widget.destroy()
        self.ai_cards = {}

        # Create cards for all known AIs
        for name, info in KNOWN_AIS.items():
            detected = self.detected_ais.get(name, False)

            config = {
                "command": info["default_command"],
                "color": info["color"],
                "enabled": detected,  # Enable by default only if detected
                "description": info["description"],
            }

            card = AIConfigCard(
                self.ai_scroll.scrollable_frame,
                ai_name=name,
                config=config,
                detected=detected
            )
            card.pack(fill="x", pady=PAD["small"])

            self.ai_cards[name] = card

    # ==================== CONFIG GENERATION ====================

    def _generate_config(self) -> dict:
        """Generate configuration dictionary from wizard data."""
        # Collect AI configs
        ais = {}
        for name, card in self.ai_cards.items():
            config = card.get_config()
            ais[name] = config

        # Determine default AI - use selected or fallback to first enabled
        default_ai = self.default_ai.get()
        if default_ai == "(none detected)" or not default_ai:
            # Find first enabled AI as fallback
            for name, config in ais.items():
                if config.get("enabled", False):
                    default_ai = name
                    break
            else:
                # No enabled AI - use "claude" as last resort
                default_ai = "claude"

        return {
            "default_ai": default_ai,
            "ais": ais,
            "special_commands": {
                "debate_rounds": 3,
                "all_parallel": self.parallel.get(),
            },
            "display": {
                "show_timestamps": self.show_timestamps.get(),
                "max_width": self.max_width.get(),
                "show_ai_header": self.show_ai_header.get(),
            },
            "context": {
                "max_history": 50,
                "auto_context": 0,
            },
            "execution": {
                "streaming": self.streaming.get(),
                "parallel": self.parallel.get(),
                "refresh_rate": 10,
            },
        }

    def _update_summary(self):
        """Update the YAML preview on summary page."""
        config = self._generate_config()
        yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
        self.yaml_preview.update(yaml_str)

    def _save_config(self):
        """Save configuration to YAML file."""
        config = self._generate_config()

        # Ensure directory exists
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    def _finish_wizard(self):
        """Complete the wizard."""
        try:
            self._save_config()
            if self.on_complete:
                self.on_complete()
            self.destroy()
        except Exception as e:
            tk.messagebox.showerror("Error", f"Failed to save configuration:\n{e}")

    def _on_close(self):
        """Handle window close."""
        if tk.messagebox.askyesno("Cancel Setup", "Are you sure you want to cancel setup?"):
            self.destroy()


def run_wizard(config_path: Path = None) -> bool:
    """
    Run the setup wizard standalone.

    Returns True if wizard completed successfully, False if cancelled.
    """
    completed = False

    def on_complete():
        nonlocal completed
        completed = True

    root = tk.Tk()
    root.withdraw()

    wizard = SetupWizard(root, on_complete=on_complete, config_path=config_path)
    root.wait_window(wizard)
    root.destroy()

    return completed


if __name__ == "__main__":
    # Test the wizard standalone
    run_wizard()
