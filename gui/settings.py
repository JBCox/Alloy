"""Settings Editor for Alloy configuration."""

import os
import subprocess
import platform
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog
from pathlib import Path
from typing import Optional

import yaml

from .styles import COLORS, FONTS, PAD, WINDOW_SIZES, apply_dark_theme
from .widgets import (
    AIConfigCard, LabeledCheckbox, LabeledDropdown, LabeledSpinbox,
    LabeledEntry, LabeledFileEntry, ScrollableFrame, YAMLPreview
)
from .tooltips import ToolTip, TOOLTIPS


class SettingsEditor(tk.Toplevel):
    """Settings editor with tabbed interface."""

    def __init__(self, parent=None, config_path: Path = None):
        super().__init__(parent)

        self.config_path = config_path or Path.cwd() / "config.yaml"
        self.config_data = {}
        self.ai_cards = {}
        self.modified = False

        # Load existing config
        self._load_config()

        # Window setup
        self.title("Alloy Settings")
        self.geometry(f"{WINDOW_SIZES['settings'][0]}x{WINDOW_SIZES['settings'][1]}")
        self.configure(bg=COLORS["bg"])
        self.minsize(600, 400)

        # Center window
        self.update_idletasks()
        x = (self.winfo_screenwidth() - WINDOW_SIZES['settings'][0]) // 2
        y = (self.winfo_screenheight() - WINDOW_SIZES['settings'][1]) // 2
        self.geometry(f"+{x}+{y}")

        # Apply theme
        apply_dark_theme(self)

        # Handle close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Create UI
        self._create_ui()

    def _load_config(self):
        """Load configuration from YAML file with validation."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.config_data = yaml.safe_load(f) or {}

                # Validate structure - ensure expected keys are dicts
                for key in ["ais", "modes", "custom_templates", "display", "execution", "context"]:
                    if key in self.config_data and not isinstance(self.config_data[key], dict):
                        self.config_data[key] = {}

            except yaml.YAMLError as e:
                # Show error but continue with empty config
                self.config_data = {}
                self.after(100, lambda: tk.messagebox.showerror(
                    "Config Error",
                    f"Failed to parse config.yaml:\n{e}\n\nUsing default settings."
                ))
            except Exception as e:
                self.config_data = {}
                self.after(100, lambda: tk.messagebox.showerror(
                    "Config Error",
                    f"Failed to load config:\n{e}\n\nUsing default settings."
                ))
        else:
            self.config_data = {}

    def _create_ui(self):
        """Create the settings UI."""
        # Main container
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])

        # Header
        header = ttk.Frame(main)
        header.pack(fill="x")

        ttk.Label(header, text="Settings", style="Heading.TLabel").pack(side="left")

        # Notebook (tabs)
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True, pady=(PAD["medium"], 0))

        # Create tabs
        self.notebook.add(self._create_ais_tab(), text="  AIs  ")
        self.notebook.add(self._create_display_tab(), text="  Display  ")
        self.notebook.add(self._create_execution_tab(), text="  Execution  ")
        self.notebook.add(self._create_context_tab(), text="  Context  ")
        self.notebook.add(self._create_modes_tab(), text="  Modes  ")
        self.notebook.add(self._create_templates_tab(), text="  Templates  ")
        self.notebook.add(self._create_advanced_tab(), text="  Advanced  ")
        self.notebook.add(self._create_builder_tab(), text="  Builder  ")

        # Button bar
        self._create_button_bar(main)

    def _create_ais_tab(self) -> ttk.Frame:
        """Create AIs configuration tab."""
        tab = ttk.Frame(self.notebook)

        # Default AI at top
        top_frame = ttk.Frame(tab)
        top_frame.pack(fill="x", pady=PAD["medium"])

        ai_names = list(self.config_data.get("ais", {}).keys())
        default = self.config_data.get("default_ai", "claude")
        default_judge = self.config_data.get("default_judge", "")

        self.default_ai = LabeledDropdown(
            top_frame,
            label="Default AI:",
            options=ai_names or ["claude"],
            default=default,
            on_change=self._mark_modified
        )
        self.default_ai.pack(side="left")

        # Add default judge dropdown
        judge_options = ["(Use default AI)"] + ai_names
        self.default_judge = LabeledDropdown(
            top_frame,
            label="Default Judge AI:",
            options=judge_options,
            default=default_judge if default_judge else "(Use default AI)",
            on_change=self._mark_modified
        )
        self.default_judge.pack(side="left", padx=(PAD["large"], 0))

        # Scrollable frame for AI cards
        ttk.Label(tab, text="Configured AIs:", style="TLabel").pack(anchor="w")

        ai_scroll = ScrollableFrame(tab)
        ai_scroll.pack(fill="both", expand=True, pady=PAD["small"])

        # Create cards for each AI
        for name, config in self.config_data.get("ais", {}).items():
            card = AIConfigCard(
                ai_scroll.scrollable_frame,
                ai_name=name,
                config=config,
                detected=True,  # Assume detected since it's in config
                on_change=self._mark_modified
            )
            card.pack(fill="x", pady=PAD["small"])
            self.ai_cards[name] = card

        return tab

    def _create_display_tab(self) -> ttk.Frame:
        """Create display settings tab."""
        tab = ttk.Frame(self.notebook)

        content = ttk.Frame(tab)
        content.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])

        display = self.config_data.get("display", {})
        execution = self.config_data.get("execution", {})

        # Checkboxes
        self.show_timestamps = LabeledCheckbox(
            content,
            label="Show timestamps on messages",
            default=display.get("show_timestamps", True),
            on_change=self._mark_modified
        )
        self.show_timestamps.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.show_timestamps, TOOLTIPS.get("show_timestamps", ""))

        self.show_ai_header = LabeledCheckbox(
            content,
            label="Show AI name in response headers",
            default=display.get("show_ai_header", True),
            on_change=self._mark_modified
        )
        self.show_ai_header.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.show_ai_header, TOOLTIPS.get("show_ai_header", ""))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Verbose mode dropdown
        verbose_frame = ttk.Frame(content)
        verbose_frame.pack(anchor="w", pady=PAD["small"])

        ttk.Label(verbose_frame, text="Output verbosity:", style="TLabel").pack(side="left")
        self.verbose_mode = ttk.Combobox(
            verbose_frame,
            values=["silent", "normal", "verbose", "debug"],
            state="readonly",
            width=12
        )
        self.verbose_mode.set(display.get("verbose_mode", "normal"))
        self.verbose_mode.pack(side="left", padx=(PAD["small"], 0))
        self.verbose_mode.bind("<<ComboboxSelected>>", lambda e: self._mark_modified())
        ToolTip(self.verbose_mode, TOOLTIPS.get("verbose_mode", ""))

        # Spinboxes
        self.max_width = LabeledSpinbox(
            content,
            label="Max response width (0 = no limit):",
            from_=0,
            to=300,
            default=display.get("max_width", 120),
            on_change=self._mark_modified
        )
        self.max_width.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.max_width, TOOLTIPS.get("max_width", ""))

        self.preview_length = LabeledSpinbox(
            content,
            label="History preview length:",
            from_=50,
            to=500,
            default=display.get("preview_length", 100),
            on_change=self._mark_modified
        )
        self.preview_length.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.preview_length, TOOLTIPS.get("preview_length", ""))

        self.refresh_rate = LabeledSpinbox(
            content,
            label="Streaming refresh rate (updates/sec):",
            from_=1,
            to=30,
            default=execution.get("refresh_rate", 10),
            on_change=self._mark_modified
        )
        self.refresh_rate.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.refresh_rate, TOOLTIPS.get("refresh_rate", ""))

        return tab

    def _create_execution_tab(self) -> ttk.Frame:
        """Create execution settings tab."""
        tab = ttk.Frame(self.notebook)

        content = ttk.Frame(tab)
        content.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])

        execution = self.config_data.get("execution", {})
        special = self.config_data.get("special_commands", {})

        # Streaming & Parallel section
        ttk.Label(content, text="Execution Mode", style="Subheading.TLabel").pack(anchor="w")

        self.streaming = LabeledCheckbox(
            content,
            label="Enable streaming (show output as it generates)",
            default=execution.get("streaming", True),
            on_change=self._mark_modified
        )
        self.streaming.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.streaming, TOOLTIPS.get("streaming", ""))

        self.parallel = LabeledCheckbox(
            content,
            label="Enable parallel execution",
            default=execution.get("parallel", False),
            on_change=self._mark_modified
        )
        self.parallel.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.parallel, TOOLTIPS.get("parallel", ""))

        self.all_parallel = LabeledCheckbox(
            content,
            label="Run @all queries in parallel",
            default=special.get("all_parallel", False),
            on_change=self._mark_modified
        )
        self.all_parallel.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.all_parallel, TOOLTIPS.get("all_parallel", ""))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Timeouts & Limits section
        ttk.Label(content, text="Timeouts & Limits", style="Subheading.TLabel").pack(anchor="w")

        self.timeout = LabeledSpinbox(
            content,
            label="Global timeout (seconds):",
            from_=30,
            to=600,
            default=execution.get("timeout", 300),
            on_change=self._mark_modified
        )
        self.timeout.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.timeout, TOOLTIPS.get("timeout", ""))

        self.max_concurrent_ais = LabeledSpinbox(
            content,
            label="Max concurrent AIs:",
            from_=1,
            to=10,
            default=execution.get("max_concurrent_ais", 3),
            on_change=self._mark_modified
        )
        self.max_concurrent_ais.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.max_concurrent_ais, TOOLTIPS.get("max_concurrent_ais", ""))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Retry section
        ttk.Label(content, text="Retry Behavior", style="Subheading.TLabel").pack(anchor="w")

        self.retry_count = LabeledSpinbox(
            content,
            label="Retry count (0 = no retries):",
            from_=0,
            to=5,
            default=execution.get("retry_count", 0),
            on_change=self._mark_modified
        )
        self.retry_count.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.retry_count, TOOLTIPS.get("retry_count", ""))

        self.retry_delay = LabeledSpinbox(
            content,
            label="Retry delay (seconds):",
            from_=1,
            to=10,
            default=execution.get("retry_delay", 2),
            on_change=self._mark_modified
        )
        self.retry_delay.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.retry_delay, TOOLTIPS.get("retry_delay", ""))

        return tab

    def _create_context_tab(self) -> ttk.Frame:
        """Create context/history settings tab."""
        tab = ttk.Frame(self.notebook)

        content = ttk.Frame(tab)
        content.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])

        context = self.config_data.get("context", {})

        # History section
        ttk.Label(content, text="Conversation History", style="Subheading.TLabel").pack(anchor="w")

        self.max_history = LabeledSpinbox(
            content,
            label="Max history messages:",
            from_=10,
            to=200,
            default=context.get("max_history", 50),
            on_change=self._mark_modified
        )
        self.max_history.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.max_history, TOOLTIPS.get("max_history", ""))

        self.auto_context = LabeledSpinbox(
            content,
            label="Auto-include last N messages (0 = disabled):",
            from_=0,
            to=20,
            default=context.get("auto_context", 0),
            on_change=self._mark_modified
        )
        self.auto_context.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.auto_context, TOOLTIPS.get("auto_context", ""))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Save section
        ttk.Label(content, text="Session Saving", style="Subheading.TLabel").pack(anchor="w")

        self.save_conversations = LabeledCheckbox(
            content,
            label="Auto-save conversations",
            default=context.get("save_conversations", False),
            on_change=self._mark_modified
        )
        self.save_conversations.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.save_conversations, TOOLTIPS.get("save_conversations", ""))

        self.save_path = LabeledFileEntry(
            content,
            label="Save directory:",
            default=context.get("save_path", ""),
            mode="directory",
            on_change=self._mark_modified
        )
        self.save_path.pack(anchor="w", fill="x", pady=PAD["small"])
        ToolTip(self.save_path, TOOLTIPS.get("save_path", ""))

        return tab

    def _create_modes_tab(self) -> ttk.Frame:
        """Create collaboration modes settings tab."""
        tab = ttk.Frame(self.notebook)

        # Use scrollable frame for potentially long content
        scroll = ScrollableFrame(tab)
        scroll.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])
        content = scroll.scrollable_frame

        modes = self.config_data.get("modes", {})
        special = self.config_data.get("special_commands", {})

        ttk.Label(content, text="Default Mode Settings", style="Subheading.TLabel").pack(anchor="w")
        ttk.Label(
            content,
            text="Configure default rounds and parallel execution for each collaboration mode.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["xs"], PAD["medium"]))

        # Mode settings - now with rounds AND parallel
        self.mode_settings = {}
        self.mode_parallel = {}

        mode_configs = [
            ("roundtable", "Roundtable", 1),
            ("chain", "Chain", 3),
            ("brainstorm", "Brainstorm", 1),
            ("devils-advocate", "Devil's Advocate", 2),
            ("code-review", "Code Review", 1),
            ("solve", "Solve", 3),
        ]

        for mode_name, label, default in mode_configs:
            mode_data = modes.get(mode_name, {})

            # Container for each mode
            mode_frame = ttk.Frame(content, style="Light.TFrame")
            mode_frame.pack(fill="x", pady=PAD["small"])

            inner = ttk.Frame(mode_frame, style="Light.TFrame")
            inner.pack(fill="x", padx=PAD["small"], pady=PAD["small"])

            ttk.Label(inner, text=f"{label}:", style="Subheading.TLabel").pack(anchor="w")

            options_frame = ttk.Frame(inner, style="Light.TFrame")
            options_frame.pack(fill="x", pady=(PAD["xs"], 0))

            # Rounds spinbox
            spinbox = LabeledSpinbox(
                options_frame,
                label="Rounds:",
                from_=1,
                to=10,
                default=mode_data.get("rounds", default),
                on_change=self._mark_modified
            )
            spinbox.pack(side="left")
            self.mode_settings[mode_name] = spinbox

            # Parallel checkbox
            parallel_cb = LabeledCheckbox(
                options_frame,
                label="Run in parallel",
                default=mode_data.get("parallel", False),
                on_change=self._mark_modified
            )
            parallel_cb.pack(side="left", padx=(PAD["large"], 0))
            self.mode_parallel[mode_name] = parallel_cb

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Global mode settings
        global_data = modes.get("global", {})

        ttk.Label(content, text="Global Mode Settings", style="Subheading.TLabel").pack(anchor="w")
        ttk.Label(
            content,
            text="Settings that apply across all collaboration modes.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["xs"], PAD["medium"]))

        self.satisfaction_threshold = LabeledSpinbox(
            content,
            label="Satisfaction threshold (1-10):",
            from_=1,
            to=10,
            default=global_data.get("satisfaction_threshold", 8),
            on_change=self._mark_modified
        )
        self.satisfaction_threshold.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.satisfaction_threshold, TOOLTIPS.get("satisfaction_threshold", ""))

        self.max_rounds = LabeledSpinbox(
            content,
            label="Max rounds safety cap:",
            from_=1,
            to=20,
            default=global_data.get("max_rounds", 10),
            on_change=self._mark_modified
        )
        self.max_rounds.pack(anchor="w", pady=PAD["small"])
        ToolTip(self.max_rounds, TOOLTIPS.get("max_rounds", ""))

        # Voting system dropdown
        voting_frame = ttk.Frame(content)
        voting_frame.pack(anchor="w", pady=PAD["small"])

        ttk.Label(voting_frame, text="Voting system:", style="TLabel").pack(side="left")
        self.voting_system = ttk.Combobox(
            voting_frame,
            values=["majority", "unanimous", "weighted"],
            state="readonly",
            width=12
        )
        self.voting_system.set(global_data.get("voting_system", "majority"))
        self.voting_system.pack(side="left", padx=(PAD["small"], 0))
        self.voting_system.bind("<<ComboboxSelected>>", lambda e: self._mark_modified())
        ToolTip(self.voting_system, TOOLTIPS.get("voting_system", ""))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Legacy setting
        self.debate_rounds = LabeledSpinbox(
            content,
            label="Debate rounds (legacy):",
            from_=1,
            to=10,
            default=special.get("debate_rounds", 3),
            on_change=self._mark_modified
        )
        self.debate_rounds.pack(anchor="w", pady=PAD["small"])

        return tab

    def _create_advanced_tab(self) -> ttk.Frame:
        """Create advanced settings tab with config management."""
        tab = ttk.Frame(self.notebook)

        content = ttk.Frame(tab)
        content.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])

        # Config file section
        ttk.Label(content, text="Configuration File", style="Subheading.TLabel").pack(anchor="w")

        path_frame = ttk.Frame(content)
        path_frame.pack(fill="x", pady=PAD["small"])

        ttk.Label(path_frame, text="Path:", style="TLabel").pack(side="left")
        path_entry = ttk.Entry(path_frame, width=50, state="readonly")
        path_entry.pack(side="left", padx=(PAD["small"], 0), fill="x", expand=True)
        path_entry.configure(state="normal")
        path_entry.insert(0, str(self.config_path))
        path_entry.configure(state="readonly")

        # Config actions
        btn_frame = ttk.Frame(content)
        btn_frame.pack(anchor="w", pady=PAD["small"])

        ttk.Button(
            btn_frame,
            text="Open in Editor",
            command=self._open_in_editor
        ).pack(side="left", padx=(0, PAD["small"]))

        ttk.Button(
            btn_frame,
            text="Open Config Folder",
            command=self._open_config_folder
        ).pack(side="left", padx=(0, PAD["small"]))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Import/Export section
        ttk.Label(content, text="Import / Export", style="Subheading.TLabel").pack(anchor="w")

        io_frame = ttk.Frame(content)
        io_frame.pack(anchor="w", pady=PAD["small"])

        ttk.Button(
            io_frame,
            text="Export Config...",
            command=self._export_config
        ).pack(side="left", padx=(0, PAD["small"]))

        ttk.Button(
            io_frame,
            text="Import Config...",
            command=self._import_config
        ).pack(side="left")

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Reset section
        ttk.Label(content, text="Reset", style="Subheading.TLabel").pack(anchor="w")

        ttk.Label(
            content,
            text="Reset all settings to their default values. This cannot be undone.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["xs"], PAD["small"]))

        ttk.Button(
            content,
            text="Reset All to Defaults",
            command=self._reset_to_defaults,
            style="Danger.TButton"
        ).pack(anchor="w")

        # Version info at bottom
        ttk.Frame(content).pack(fill="both", expand=True)  # Spacer
        ttk.Label(
            content,
            text="Alloy - Multiple AIs, stronger together",
            style="Dim.TLabel"
        ).pack(side="bottom")

        return tab

    def _open_in_editor(self):
        """Open config file in system default editor."""
        try:
            if platform.system() == "Windows":
                os.startfile(str(self.config_path))
            elif platform.system() == "Darwin":  # macOS
                subprocess.run(["open", str(self.config_path)], check=True)
            else:  # Linux
                subprocess.run(["xdg-open", str(self.config_path)], check=True)
        except Exception as e:
            tk.messagebox.showerror("Error", f"Failed to open editor: {e}")

    def _open_config_folder(self):
        """Open the folder containing the config file."""
        try:
            folder = self.config_path.parent
            if platform.system() == "Windows":
                os.startfile(str(folder))
            elif platform.system() == "Darwin":  # macOS
                subprocess.run(["open", str(folder)], check=True)
            else:  # Linux
                subprocess.run(["xdg-open", str(folder)], check=True)
        except Exception as e:
            tk.messagebox.showerror("Error", f"Failed to open folder: {e}")

    def _export_config(self):
        """Export current config to a file."""
        path = filedialog.asksaveasfilename(
            defaultextension=".yaml",
            filetypes=[("YAML files", "*.yaml"), ("All files", "*.*")],
            initialfile="alloy_config_export.yaml",
            title="Export Configuration"
        )
        if path:
            try:
                config = self._collect_config()
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                tk.messagebox.showinfo("Export Complete", f"Configuration exported to:\n{path}")
            except Exception as e:
                tk.messagebox.showerror("Export Error", f"Failed to export: {e}")

    def _import_config(self):
        """Import config from a file."""
        path = filedialog.askopenfilename(
            filetypes=[("YAML files", "*.yaml"), ("All files", "*.*")],
            title="Import Configuration"
        )
        if path:
            if not tk.messagebox.askyesno(
                "Import Configuration",
                "This will replace all current settings with the imported ones.\n\nContinue?"
            ):
                return

            try:
                with open(path, "r", encoding="utf-8") as f:
                    new_config = yaml.safe_load(f)

                if not isinstance(new_config, dict):
                    raise ValueError("Invalid configuration file format")

                # Save to file and reload
                with open(self.config_path, "w", encoding="utf-8") as f:
                    yaml.dump(new_config, f, default_flow_style=False, sort_keys=False)

                self.config_data = new_config
                tk.messagebox.showinfo(
                    "Import Complete",
                    "Configuration imported. Please close and reopen settings to see changes."
                )
                self.destroy()

            except Exception as e:
                tk.messagebox.showerror("Import Error", f"Failed to import: {e}")

    def _reset_to_defaults(self):
        """Reset all settings to defaults."""
        if not tk.messagebox.askyesno(
            "Reset to Defaults",
            "This will reset ALL settings to their default values.\n\n"
            "This cannot be undone. Continue?"
        ):
            return

        # Create default config
        from config import Config
        default = Config._default_config()
        default.save(self.config_path)

        tk.messagebox.showinfo(
            "Reset Complete",
            "Settings have been reset to defaults.\n"
            "Please close and reopen settings to see changes."
        )
        self.destroy()

    def _create_builder_tab(self) -> ttk.Frame:
        """Collaborative Model Builder settings (the ``builder`` key; spec D9).

        Only the keys shown here are collected; everything else under ``builder``
        (presets, deadlines, provider timeouts, workflow_root) survives through the
        deep merge in ``_merged_config``.
        """
        tab = ttk.Frame(self.notebook)
        scroll = ScrollableFrame(tab)
        scroll.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])
        content = scroll.scrollable_frame

        builder = self.config_data.get("builder")
        builder = builder if isinstance(builder, dict) else {}

        def section(data, key):
            value = data.get(key)
            return value if isinstance(value, dict) else {}

        blender = section(builder, "blender")
        agents = section(builder, "agents")
        limits = section(builder, "limits")
        concept = section(builder, "concept")
        image_gen = section(builder, "image_generation")
        w = self.builder_widgets = {}

        def add(key, widget):
            widget.pack(anchor="w", fill="x", pady=PAD["xs"])
            w[key] = widget
            return widget

        ttk.Label(content, text="Collaborative Model Builder", style="Subheading.TLabel").pack(anchor="w")
        ttk.Label(content, text="Used by `python main.py --build ...` and the Model Builder window. "
                                "The chat modes never read these settings.", wraplength=600).pack(anchor="w")
        add("attended", LabeledCheckbox(content, label="Attended by default (the run stops for your acceptance before "
                                                       "advancing past a detailed component)",
                                        default=bool(builder.get("attended", True)), on_change=self._mark_modified))
        add("isolated_reviews", LabeledDropdown(content, label="Isolated reviews:", options=["when_contaminated", "always"],
                                                default=str(builder.get("isolated_reviews") or "when_contaminated"),
                                                on_change=self._mark_modified))
        add("blender.executable", LabeledFileEntry(content, label="Blender executable (empty = auto-detect):",
                                                   default=str(blender.get("executable") or ""), is_directory=False,
                                                   on_change=self._mark_modified))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])
        ttk.Label(content, text="Agents (seats A and B)", style="Subheading.TLabel").pack(anchor="w")
        for label in ("A", "B"):
            agent = section(agents, label)
            row = ttk.Frame(content)
            row.pack(fill="x", pady=PAD["xs"])
            ttk.Label(row, text=f"Seat {label}").pack(anchor="w")
            grid = ttk.Frame(row)
            grid.pack(fill="x")
            for col, (key, text, width) in enumerate((("provider", "provider:", 12), ("model", "model:", 18),
                                                       ("reasoning", "reasoning:", 10))):
                widget = LabeledEntry(grid, label=text, default=str(agent.get(key) or ""), width=width,
                                      on_change=self._mark_modified)
                widget.grid(row=0, column=col, padx=(0, PAD["small"]), sticky="w")
                w[f"agents.{label}.{key}"] = widget
            add(f"agents.{label}.executable", LabeledFileEntry(content, label=f"Seat {label} executable (empty = PATH):",
                                                              default=str(agent.get("executable") or ""),
                                                              is_directory=False, on_change=self._mark_modified))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])
        ttk.Label(content, text="Limits (0 = unlimited)", style="Subheading.TLabel").pack(anchor="w")
        for key, text, hi in (("wall_clock_minutes", "Wall clock (minutes):", 100000), ("max_requests", "Max provider requests:", 100000),
                              ("max_renders", "Max renders:", 100000), ("attempts_per_finding", "Correction attempts per finding:", 20)):
            add(f"limits.{key}", LabeledSpinbox(content, label=text, from_=0, to=hi, default=int(limits.get(key) or 0),
                                                on_change=self._mark_modified))
        add("limits.max_cost_usd", LabeledEntry(content, label="Max cost (USD; enforceable only where the provider reports "
                                                              "cost, otherwise labelled unenforceable):",
                                                default=str(limits.get("max_cost_usd") if limits.get("max_cost_usd") is not None else 0),
                                                width=12, on_change=self._mark_modified))

        ttk.Separator(content, orient="horizontal").pack(fill="x", pady=PAD["medium"])
        ttk.Label(content, text="Concept stage (image generation is manual: you generate in your app and import)",
                  style="Subheading.TLabel", wraplength=600).pack(anchor="w")
        add("concept.approval", LabeledDropdown(content, label="Approval mode:", options=["each", "anchor_only", "auto"],
                                                default=str(concept.get("approval") or "each"), on_change=self._mark_modified))
        add("concept.anchor_candidates", LabeledSpinbox(content, label="Anchor candidates:", from_=1, to=12,
                                                        default=int(concept.get("anchor_candidates") or 4), on_change=self._mark_modified))
        views = concept.get("views")
        views_text = ", ".join(str(v) for v in views) if isinstance(views, list) else "front, side, rear, top, underside, three-quarter"
        add("concept.views", LabeledEntry(content, label="Turnaround views (comma-separated):", default=views_text, width=60,
                                          on_change=self._mark_modified))
        add("concept.max_images", LabeledSpinbox(content, label="Max images (imported and generated alike):", from_=1, to=10000,
                                                 default=int(concept.get("max_images") or 40), on_change=self._mark_modified))
        add("concept.max_regenerations_per_view", LabeledSpinbox(content, label="Max regenerations per view:", from_=0, to=100,
                                                                 default=int(concept.get("max_regenerations_per_view") or 3),
                                                                 on_change=self._mark_modified))
        add("concept.import_dir", LabeledFileEntry(content, label="Import directory (empty = <workflow>\\concept\\imports):",
                                                   default=str(concept.get("import_dir") or ""), is_directory=True,
                                                   on_change=self._mark_modified))
        add("image_generation.seat", LabeledDropdown(content, label="Image seat:", options=["manual"],
                                                     default=str(image_gen.get("seat") or "manual"), on_change=self._mark_modified))
        add("image_generation.vendor", LabeledDropdown(content, label="Intended vendor (declared, never verified):",
                                                       options=["chatgpt", "gemini", "other"],
                                                       default=str(image_gen.get("vendor") or "chatgpt"), on_change=self._mark_modified))
        add("image_generation.model", LabeledEntry(content, label="Image model as shown in the app (declaration):",
                                                   default=str(image_gen.get("model") or ""), width=30, on_change=self._mark_modified))
        return tab

    def _collect_builder(self) -> dict:
        """The builder keys the Builder tab exposes; merged onto the rest of ``builder`` on save."""
        w = self.builder_widgets

        def number(text):
            try:
                value = float(text)
            except (TypeError, ValueError):
                return 0
            return int(value) if value.is_integer() else value

        return {
            "attended": bool(w["attended"].get()),
            "isolated_reviews": w["isolated_reviews"].get(),
            "blender": {"executable": w["blender.executable"].get()},
            "agents": {label: {key: w[f"agents.{label}.{key}"].get() for key in ("provider", "model", "reasoning", "executable")}
                       for label in ("A", "B")},
            "limits": {
                "wall_clock_minutes": w["limits.wall_clock_minutes"].get(),
                "max_cost_usd": number(w["limits.max_cost_usd"].get()),
                "max_requests": w["limits.max_requests"].get(),
                "max_renders": w["limits.max_renders"].get(),
                "attempts_per_finding": w["limits.attempts_per_finding"].get(),
            },
            "concept": {
                "approval": w["concept.approval"].get(),
                "anchor_candidates": w["concept.anchor_candidates"].get(),
                "views": [v.strip() for v in w["concept.views"].get().split(",") if v.strip()],
                "max_images": w["concept.max_images"].get(),
                "max_regenerations_per_view": w["concept.max_regenerations_per_view"].get(),
                "import_dir": w["concept.import_dir"].get(),
            },
            "image_generation": {
                "seat": w["image_generation.seat"].get(),
                "vendor": w["image_generation.vendor"].get(),
                "model": w["image_generation.model"].get(),
            },
        }

    def _create_templates_tab(self) -> ttk.Frame:
        """Create custom role templates tab."""
        tab = ttk.Frame(self.notebook)

        content = ttk.Frame(tab)
        content.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["medium"])

        ttk.Label(content, text="Custom Role Templates", style="Subheading.TLabel").pack(anchor="w")
        ttk.Label(
            content,
            text="Create custom role templates for @roles mode. Each template defines roles for AIs.",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["xs"], PAD["medium"]))

        # Main layout: list on left, editor on right
        main_frame = ttk.Frame(content)
        main_frame.pack(fill="both", expand=True)

        # Left side: template list
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(side="left", fill="y", padx=(0, PAD["medium"]))

        ttk.Label(list_frame, text="Templates:", style="TLabel").pack(anchor="w")

        # Listbox with scrollbar
        list_container = ttk.Frame(list_frame)
        list_container.pack(fill="both", expand=True, pady=(PAD["xs"], 0))

        self.template_listbox = tk.Listbox(
            list_container,
            width=20,
            height=10,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            selectbackground=COLORS["accent"],
            font=FONTS["body"]
        )
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.template_listbox.yview)
        self.template_listbox.configure(yscrollcommand=scrollbar.set)

        self.template_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.template_listbox.bind("<<ListboxSelect>>", self._on_template_select)

        # Template list buttons
        btn_frame = ttk.Frame(list_frame)
        btn_frame.pack(fill="x", pady=(PAD["small"], 0))

        ttk.Button(btn_frame, text="Add", command=self._add_template, width=8).pack(side="left")
        ttk.Button(btn_frame, text="Delete", command=self._delete_template, width=8).pack(side="left", padx=(PAD["small"], 0))

        # Right side: template editor
        self.editor_frame = ttk.Frame(main_frame)
        self.editor_frame.pack(side="left", fill="both", expand=True)

        ttk.Label(self.editor_frame, text="Edit Template:", style="TLabel").pack(anchor="w")

        # Template name
        self.template_name = LabeledEntry(
            self.editor_frame,
            label="Template Name:",
            default="",
            width=30,
            on_change=self._mark_modified
        )
        self.template_name.pack(anchor="w", pady=PAD["small"])

        # Roles editor
        ttk.Label(self.editor_frame, text="Roles (one per line: role_name: description):", style="TLabel").pack(anchor="w", pady=(PAD["small"], 0))

        roles_container = ttk.Frame(self.editor_frame)
        roles_container.pack(fill="both", expand=True, pady=(PAD["xs"], 0))

        self.roles_text = tk.Text(
            roles_container,
            width=40,
            height=8,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["fg"],
            font=FONTS["mono"]
        )
        roles_scrollbar = ttk.Scrollbar(roles_container, orient="vertical", command=self.roles_text.yview)
        self.roles_text.configure(yscrollcommand=roles_scrollbar.set)

        self.roles_text.pack(side="left", fill="both", expand=True)
        roles_scrollbar.pack(side="right", fill="y")

        self.roles_text.bind("<KeyRelease>", lambda e: self._mark_modified())

        # Store template data
        self.custom_templates = {}
        self.current_template = None

        # Load existing templates
        for name, tmpl_data in self.config_data.get("custom_templates", {}).items():
            if isinstance(tmpl_data, dict):
                self.custom_templates[name] = tmpl_data.get("roles", {})
                self.template_listbox.insert("end", name)

        return tab

    def _on_template_select(self, event):
        """Handle template selection."""
        selection = self.template_listbox.curselection()
        if not selection:
            return

        # Save current template first
        self._save_current_template()

        # Load selected template
        name = self.template_listbox.get(selection[0])
        self.current_template = name
        self.template_name.set(name)

        # Load roles
        roles = self.custom_templates.get(name, {})
        self.roles_text.delete("1.0", "end")
        for role_name, desc in roles.items():
            self.roles_text.insert("end", f"{role_name}: {desc}\n")

    def _save_current_template(self):
        """Save the currently edited template."""
        if self.current_template is None:
            return

        new_name = self.template_name.get().strip()
        if not new_name:
            return

        # Parse roles from text
        roles = {}
        text = self.roles_text.get("1.0", "end").strip()
        for line in text.split("\n"):
            if ":" in line:
                role_name, desc = line.split(":", 1)
                roles[role_name.strip()] = desc.strip()

        # If name changed, update listbox and dict
        if new_name != self.current_template:
            old_name = self.current_template

            # Save new data FIRST (before deleting old)
            self.custom_templates[new_name] = roles

            # Then remove old entry
            if old_name in self.custom_templates and old_name != new_name:
                del self.custom_templates[old_name]

            # Update listbox
            for i in range(self.template_listbox.size()):
                if self.template_listbox.get(i) == old_name:
                    self.template_listbox.delete(i)
                    self.template_listbox.insert(i, new_name)
                    self.template_listbox.selection_set(i)
                    break

            self.current_template = new_name
        else:
            # Name unchanged, just save roles
            self.custom_templates[new_name] = roles

    def _add_template(self):
        """Add a new template."""
        # Save current first
        self._save_current_template()

        # Generate unique name
        base = "new_template"
        name = base
        counter = 1
        while name in self.custom_templates:
            name = f"{base}_{counter}"
            counter += 1

        self.custom_templates[name] = {}
        self.template_listbox.insert("end", name)

        # Select the new template
        self.template_listbox.selection_clear(0, "end")
        self.template_listbox.selection_set("end")
        self.current_template = name
        self.template_name.set(name)
        self.roles_text.delete("1.0", "end")

        self._mark_modified()

    def _delete_template(self):
        """Delete the selected template."""
        selection = self.template_listbox.curselection()
        if not selection:
            return

        name = self.template_listbox.get(selection[0])

        if name in self.custom_templates:
            del self.custom_templates[name]

        self.template_listbox.delete(selection[0])
        self.current_template = None
        self.template_name.set("")
        self.roles_text.delete("1.0", "end")

        self._mark_modified()

    def _create_button_bar(self, parent):
        """Create button bar with proper UX order and keyboard shortcuts."""
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(PAD["medium"], 0))

        # Left side - Cancel button (destructive actions on left)
        ttk.Button(bar, text="Cancel", command=self._cancel).pack(side="left")

        # Config path indicator
        ttk.Label(
            bar,
            text=f"Config: {self.config_path}",
            style="Dim.TLabel"
        ).pack(side="left", padx=(PAD["medium"], 0))

        # Right side - Save (primary), Apply (secondary)
        # Packed in reverse order since we're using side="right"
        self.save_btn = ttk.Button(bar, text="Save", command=self._save, style="Accent.TButton")
        self.save_btn.pack(side="right")

        ttk.Button(bar, text="Apply", command=self._apply).pack(side="right", padx=(0, PAD["small"]))

        # Keyboard shortcuts
        self.bind_all("<Control-s>", lambda e: self._save())
        self.bind_all("<Escape>", lambda e: self._cancel())

    def _mark_modified(self):
        """Mark config as modified."""
        self.modified = True
        self.title("Alloy Settings *")

    def _collect_config(self) -> dict:
        """Collect all settings into a config dictionary."""
        # Collect AI configs
        ais = {}
        for name, card in self.ai_cards.items():
            ais[name] = card.get_config()

        # Collect mode settings (rounds + parallel + global)
        modes = {}
        for mode_name, spinbox in self.mode_settings.items():
            parallel = self.mode_parallel[mode_name].get() if mode_name in self.mode_parallel else False
            modes[mode_name] = {
                "rounds": spinbox.get(),
                "parallel": parallel,
            }

        # Add global mode settings
        modes["global"] = {
            "satisfaction_threshold": self.satisfaction_threshold.get(),
            "max_rounds": self.max_rounds.get(),
            "voting_system": self.voting_system.get(),
        }

        # Collect custom templates
        self._save_current_template()  # Save any current edits
        custom_templates = {}
        for name, roles in self.custom_templates.items():
            custom_templates[name] = {"roles": roles}

        # Get default judge (empty string if "(Use default AI)")
        judge = self.default_judge.get()
        default_judge = "" if judge == "(Use default AI)" else judge

        return {
            "default_ai": self.default_ai.get(),
            "default_judge": default_judge,
            "ais": ais,
            "modes": modes,
            "custom_templates": custom_templates,
            "special_commands": {
                "debate_rounds": self.debate_rounds.get(),
                "all_parallel": self.all_parallel.get(),
            },
            "display": {
                "show_timestamps": self.show_timestamps.get(),
                "max_width": self.max_width.get(),
                "show_ai_header": self.show_ai_header.get(),
                "verbose_mode": self.verbose_mode.get(),
                "preview_length": self.preview_length.get(),
            },
            "context": {
                "max_history": self.max_history.get(),
                "auto_context": self.auto_context.get(),
                "save_conversations": self.save_conversations.get(),
                "save_path": self.save_path.get(),
            },
            "execution": {
                "streaming": self.streaming.get(),
                "parallel": self.parallel.get(),
                "refresh_rate": self.refresh_rate.get(),
                "timeout": self.timeout.get(),
                "max_concurrent_ais": self.max_concurrent_ais.get(),
                "retry_count": self.retry_count.get(),
                "retry_delay": self.retry_delay.get(),
            },
            "builder": self._collect_builder(),
        }

    def _validate(self) -> Optional[str]:
        """Validate all settings. Returns error message or None."""
        for name, card in self.ai_cards.items():
            error = card.validate()
            if error:
                return error
        return None

    def _save_to_file(self):
        """Save config to file."""
        config = self._collect_config()

        # Ensure directory exists
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        self.modified = False
        self.title("Alloy Settings")

    def _apply(self):
        """Apply changes without closing."""
        error = self._validate()
        if error:
            tk.messagebox.showerror("Validation Error", error)
            return

        try:
            self._save_to_file()
            tk.messagebox.showinfo("Saved", "Settings saved. Restart Alloy to apply changes.")
        except Exception as e:
            tk.messagebox.showerror("Error", f"Failed to save: {e}")

    def _save(self):
        """Save and close."""
        error = self._validate()
        if error:
            tk.messagebox.showerror("Validation Error", error)
            return

        try:
            self._save_to_file()
            self.destroy()
        except Exception as e:
            tk.messagebox.showerror("Error", f"Failed to save: {e}")

    def _cancel(self):
        """Cancel and close."""
        if self.modified:
            if not tk.messagebox.askyesno("Unsaved Changes",
                "You have unsaved changes. Discard them?"):
                return
        self.destroy()

    def _on_close(self):
        """Handle window close."""
        self._cancel()


def run_settings(config_path: Path = None):
    """Run the settings editor standalone."""
    root = tk.Tk()
    root.withdraw()

    editor = SettingsEditor(root, config_path=config_path)
    root.wait_window(editor)
    root.destroy()


if __name__ == "__main__":
    # Test the settings editor standalone
    run_settings()
