"""Reusable custom widgets for Alloy GUI."""

import tkinter as tk
import tkinter.ttk as ttk
from typing import Callable, Optional

from .styles import COLORS, FONTS, PAD, COLOR_OPTIONS


class LabeledEntry(ttk.Frame):
    """Entry field with label."""

    def __init__(self, parent, label: str, default: str = "", width: int = 40,
                 placeholder: str = "", on_change: Callable = None):
        super().__init__(parent)

        self.on_change = on_change
        self.placeholder = placeholder

        # Label
        self.label = ttk.Label(self, text=label, style="TLabel")
        self.label.pack(anchor="w")

        # Entry
        self.var = tk.StringVar(value=default)
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(fill="x", pady=(PAD["xs"], 0))

        if on_change:
            self.var.trace_add("write", lambda *args: on_change())

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)

    def set_state(self, state: str):
        """Set state: 'normal', 'disabled', 'readonly'"""
        self.entry.configure(state=state)


class LabeledCheckbox(ttk.Frame):
    """Checkbox with label."""

    def __init__(self, parent, label: str, default: bool = False,
                 on_change: Callable = None):
        super().__init__(parent)

        self.on_change = on_change
        self.var = tk.BooleanVar(value=default)

        self.checkbox = ttk.Checkbutton(
            self,
            text=label,
            variable=self.var,
            style="TCheckbutton"
        )
        self.checkbox.pack(anchor="w")

        if on_change:
            self.var.trace_add("write", lambda *args: on_change())

    def get(self) -> bool:
        return self.var.get()

    def set(self, value: bool):
        self.var.set(value)


class LabeledDropdown(ttk.Frame):
    """Dropdown with label."""

    def __init__(self, parent, label: str, options: list[str],
                 default: str = None, on_change: Callable = None):
        super().__init__(parent)

        self.on_change = on_change
        self.options = options

        # Label
        self.label = ttk.Label(self, text=label, style="TLabel")
        self.label.pack(anchor="w")

        # Combobox
        self.var = tk.StringVar(value=default or (options[0] if options else ""))
        self.combo = ttk.Combobox(
            self,
            textvariable=self.var,
            values=options,
            state="readonly",
            width=20
        )
        self.combo.pack(fill="x", pady=(PAD["xs"], 0))

        if on_change:
            self.combo.bind("<<ComboboxSelected>>", lambda e: on_change())

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)

    def set_options(self, options: list[str]):
        """Update available options."""
        self.options = options
        self.combo.configure(values=options)


class LabeledSpinbox(ttk.Frame):
    """Spinbox with label for numeric values."""

    def __init__(self, parent, label: str, from_: int, to: int,
                 default: int = None, on_change: Callable = None):
        super().__init__(parent)

        self.on_change = on_change

        # Label
        self.label = ttk.Label(self, text=label, style="TLabel")
        self.label.pack(anchor="w")

        # Spinbox
        self.var = tk.IntVar(value=default if default is not None else from_)
        self.spinbox = ttk.Spinbox(
            self,
            from_=from_,
            to=to,
            textvariable=self.var,
            width=10
        )
        self.spinbox.pack(anchor="w", pady=(PAD["xs"], 0))

        if on_change:
            self.var.trace_add("write", lambda *args: on_change())

    def get(self) -> int:
        try:
            return self.var.get()
        except tk.TclError:
            return 0

    def set(self, value: int):
        self.var.set(value)


class ColorPicker(ttk.Frame):
    """Color picker dropdown with preview."""

    def __init__(self, parent, label: str, default: str = "white",
                 on_change: Callable = None):
        super().__init__(parent)

        self.on_change = on_change

        # Label
        self.label = ttk.Label(self, text=label, style="TLabel")
        self.label.pack(anchor="w")

        # Container for combo and preview
        container = ttk.Frame(self)
        container.pack(fill="x", pady=(PAD["xs"], 0))

        # Color preview
        self.preview = tk.Frame(container, width=20, height=20, bg=default)
        self.preview.pack(side="left", padx=(0, PAD["small"]))
        self.preview.pack_propagate(False)

        # Combobox
        color_names = [name for name, _ in COLOR_OPTIONS]
        self.var = tk.StringVar(value=self._color_to_name(default))
        self.combo = ttk.Combobox(
            container,
            textvariable=self.var,
            values=color_names,
            state="readonly",
            width=15
        )
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", self._on_select)

    def _color_to_name(self, color: str) -> str:
        """Convert color value to display name."""
        for name, value in COLOR_OPTIONS:
            if value == color:
                return name
        return "White"

    def _name_to_color(self, name: str) -> str:
        """Convert display name to color value."""
        for n, value in COLOR_OPTIONS:
            if n == name:
                return value
        return "white"

    def _on_select(self, event):
        color = self._name_to_color(self.var.get())
        self.preview.configure(bg=color)
        if self.on_change:
            self.on_change()

    def get(self) -> str:
        return self._name_to_color(self.var.get())

    def set(self, value: str):
        self.var.set(self._color_to_name(value))
        self.preview.configure(bg=value)


class AIConfigCard(ttk.Frame):
    """Card widget for configuring a single AI."""

    def __init__(self, parent, ai_name: str, config: dict = None,
                 detected: bool = False, on_change: Callable = None):
        super().__init__(parent, style="Light.TFrame")

        self.ai_name = ai_name
        self.on_change = on_change
        self.detected = detected

        config = config or {}

        # Card padding
        inner = ttk.Frame(self, style="Light.TFrame")
        inner.pack(fill="x", padx=PAD["medium"], pady=PAD["medium"])

        # Header row: enable checkbox + name + status
        header = ttk.Frame(inner, style="Light.TFrame")
        header.pack(fill="x")

        self.enabled_var = tk.BooleanVar(value=config.get("enabled", True))
        self.enabled_cb = ttk.Checkbutton(
            header,
            variable=self.enabled_var,
            style="TCheckbutton"
        )
        self.enabled_cb.pack(side="left")

        name_label = ttk.Label(
            header,
            text=ai_name.capitalize(),
            style="Subheading.TLabel"
        )
        name_label.pack(side="left", padx=(PAD["small"], 0))

        # Status indicator
        status_text = "Detected" if detected else "Not found"
        status_style = "Success.TLabel" if detected else "Dim.TLabel"
        self.status_label = ttk.Label(header, text=f"({status_text})", style=status_style)
        self.status_label.pack(side="left", padx=(PAD["small"], 0))

        if on_change:
            self.enabled_var.trace_add("write", lambda *args: on_change())

        # Command input
        cmd_frame = ttk.Frame(inner, style="Light.TFrame")
        cmd_frame.pack(fill="x", pady=(PAD["small"], 0))

        ttk.Label(cmd_frame, text="Command:", style="TLabel").pack(anchor="w")
        self.command_var = tk.StringVar(value=config.get("command", ""))
        self.command_entry = ttk.Entry(cmd_frame, textvariable=self.command_var, width=50)
        self.command_entry.pack(fill="x", pady=(PAD["xs"], 0))

        if on_change:
            self.command_var.trace_add("write", lambda *args: on_change())

        # Color and description row
        options_frame = ttk.Frame(inner, style="Light.TFrame")
        options_frame.pack(fill="x", pady=(PAD["small"], 0))

        # Color picker
        self.color_picker = ColorPicker(
            options_frame,
            label="Color:",
            default=config.get("color", "white"),
            on_change=on_change
        )
        self.color_picker.pack(side="left")

        # Description
        desc_frame = ttk.Frame(options_frame, style="Light.TFrame")
        desc_frame.pack(side="left", padx=(PAD["large"], 0), fill="x", expand=True)

        ttk.Label(desc_frame, text="Description:", style="TLabel").pack(anchor="w")
        self.desc_var = tk.StringVar(value=config.get("description", ""))
        self.desc_entry = ttk.Entry(desc_frame, textvariable=self.desc_var, width=30)
        self.desc_entry.pack(fill="x", pady=(PAD["xs"], 0))

        if on_change:
            self.desc_var.trace_add("write", lambda *args: on_change())

    def get_config(self) -> dict:
        """Get the configuration for this AI."""
        return {
            "command": self.command_var.get(),
            "color": self.color_picker.get(),
            "enabled": self.enabled_var.get(),
            "description": self.desc_var.get(),
        }

    def set_enabled(self, enabled: bool):
        self.enabled_var.set(enabled)

    def validate(self) -> Optional[str]:
        """Validate the configuration. Returns error message or None."""
        if self.enabled_var.get():
            cmd = self.command_var.get().strip()
            if not cmd:
                return f"{self.ai_name}: Command is required"
            if "{message}" not in cmd:
                return f"{self.ai_name}: Command must contain {{message}} placeholder"
        return None


class ScrollableFrame(ttk.Frame):
    """A frame with a scrollbar for scrollable content."""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)

        # Canvas for scrolling
        self.canvas = tk.Canvas(self, bg=COLORS["bg"], highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window(
            (0, 0),
            window=self.scrollable_frame,
            anchor="nw"
        )

        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # Pack
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        # Bind mouse wheel only when mouse is over this widget (not globally)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)
        self.scrollable_frame.bind("<Enter>", self._bind_mousewheel)
        self.scrollable_frame.bind("<Leave>", self._unbind_mousewheel)

        # Make inner frame expand to canvas width
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _bind_mousewheel(self, event):
        """Bind mousewheel when mouse enters this widget."""
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, event):
        """Unbind mousewheel when mouse leaves this widget."""
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def scroll_to_bottom(self):
        """Scroll to the bottom of the content."""
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def scroll_to_top(self):
        """Scroll to the top of the content."""
        self.canvas.yview_moveto(0.0)


class YAMLPreview(ttk.Frame):
    """Read-only text widget showing YAML preview."""

    def __init__(self, parent):
        super().__init__(parent)

        # Label
        ttk.Label(self, text="Configuration Preview:", style="TLabel").pack(anchor="w")

        # Text widget with scrollbar
        text_frame = ttk.Frame(self)
        text_frame.pack(fill="both", expand=True, pady=(PAD["small"], 0))

        self.text = tk.Text(
            text_frame,
            wrap="none",
            font=FONTS["mono"],
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["fg"],
            state="disabled",
            height=15
        )

        scrollbar_y = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        scrollbar_x = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        scrollbar_y.pack(side="right", fill="y")
        scrollbar_x.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)

    def update(self, yaml_content: str):
        """Update the preview content."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", yaml_content)
        self.text.configure(state="disabled")


class StepIndicator(ttk.Frame):
    """Visual step indicator for wizards (Step 2 of 5 with dots)."""

    def __init__(self, parent, total_steps: int, step_names: list[str] = None):
        super().__init__(parent)

        self.total_steps = total_steps
        self.step_names = step_names or [f"Step {i+1}" for i in range(total_steps)]
        self.current_step = 0

        # Container for dots and text
        self.inner = ttk.Frame(self)
        self.inner.pack()

        # Step text label
        self.step_label = ttk.Label(
            self.inner,
            text="",
            style="TLabel"
        )
        self.step_label.pack()

        # Dots container
        self.dots_frame = ttk.Frame(self.inner)
        self.dots_frame.pack(pady=(PAD["xs"], 0))

        # Create dot labels
        self.dots = []
        for i in range(total_steps):
            dot = tk.Label(
                self.dots_frame,
                text="●" if i == 0 else "○",
                font=FONTS["body"],
                fg=COLORS["accent"] if i == 0 else COLORS["fg_dim"],
                bg=COLORS["bg"],
                padx=3
            )
            dot.pack(side="left")
            self.dots.append(dot)

        # Initial update
        self.set_step(0)

    def set_step(self, step: int):
        """Set the current step (0-indexed)."""
        self.current_step = step

        # Update step text
        step_name = self.step_names[step] if step < len(self.step_names) else ""
        self.step_label.configure(text=f"{step_name} ({step + 1} of {self.total_steps})")

        # Update dots
        for i, dot in enumerate(self.dots):
            if i < step:
                # Completed step
                dot.configure(text="●", fg=COLORS["success"])
            elif i == step:
                # Current step
                dot.configure(text="●", fg=COLORS["accent"])
            else:
                # Future step
                dot.configure(text="○", fg=COLORS["fg_dim"])


class LabeledFileEntry(ttk.Frame):
    """Entry field with label and browse button for file/directory paths."""

    def __init__(self, parent, label: str, default: str = "",
                 is_directory: bool = True, on_change: Callable = None):
        super().__init__(parent)

        self.on_change = on_change
        self.is_directory = is_directory

        # Label
        self.label = ttk.Label(self, text=label, style="TLabel")
        self.label.pack(anchor="w")

        # Entry + button container
        container = ttk.Frame(self)
        container.pack(fill="x", pady=(PAD["xs"], 0))

        # Entry
        self.var = tk.StringVar(value=default)
        self.entry = ttk.Entry(container, textvariable=self.var, width=40)
        self.entry.pack(side="left", fill="x", expand=True)

        # Browse button
        self.browse_btn = ttk.Button(
            container,
            text="Browse...",
            command=self._browse,
            width=10
        )
        self.browse_btn.pack(side="left", padx=(PAD["small"], 0))

        if on_change:
            self.var.trace_add("write", lambda *args: on_change())

    def _browse(self):
        """Open file/directory dialog."""
        from tkinter import filedialog

        if self.is_directory:
            path = filedialog.askdirectory(
                initialdir=self.var.get() or "~",
                title="Select Directory"
            )
        else:
            path = filedialog.askopenfilename(
                initialdir=self.var.get() or "~",
                title="Select File"
            )

        if path:
            self.var.set(path)

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str):
        self.var.set(value)
