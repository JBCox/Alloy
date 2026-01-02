"""Main GUI Application for Alloy.

This module provides a full graphical interface for Alloy,
allowing users to interact with multiple AIs through a
modern chat-style interface with split-view support.
"""

import threading
import queue
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import messagebox
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from dataclasses import dataclass, field

from config import Config
from orchestrator import Orchestrator, AIResponse
from router import Router, CommandType
from modes import ModeType, ModeConfig, ROLE_TEMPLATES

from .styles import COLORS, FONTS, PAD, apply_dark_theme
from .widgets import ScrollableFrame
from .tooltips import ToolTip
from .settings import SettingsEditor


# Window configuration
WINDOW_SIZE = (1200, 800)
WINDOW_MIN_SIZE = (900, 600)
SIDEBAR_WIDTH = 200


@dataclass
class Message:
    """A message in the conversation."""
    role: str  # "user", "ai", "system", "error"
    content: str
    ai_name: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


class AlloyGUI(tk.Tk):
    """Main Alloy GUI Application."""

    def __init__(self, config_path: Optional[Path] = None):
        super().__init__()

        # Load configuration
        self.config = Config.load(config_path)
        self.config_path = config_path or Config.get_config_path()

        # Core components
        self.orchestrator = Orchestrator(self.config)
        self.router = Router(self.config)

        # State
        self.messages: list[Message] = []
        self.current_ai = self.config.default_ai
        self.current_mode = "direct"  # "direct", "roundtable", "chain", etc.
        self.is_processing = False
        self.abort_requested = False
        self.view_mode = "single"  # "single" or "split"

        # Thread communication
        self.response_queue = queue.Queue()

        # Window setup
        self.title("Alloy - Multiple AIs, stronger together")
        self.geometry(f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}")
        self.minsize(*WINDOW_MIN_SIZE)
        self.configure(bg=COLORS["bg"])

        # Center window
        self.update_idletasks()
        x = (self.winfo_screenwidth() - WINDOW_SIZE[0]) // 2
        y = (self.winfo_screenheight() - WINDOW_SIZE[1]) // 2
        self.geometry(f"+{x}+{y}")

        # Apply theme
        apply_dark_theme(self)

        # Build UI
        self._create_menu()
        self._create_layout()
        self._create_status_bar()

        # Keyboard shortcuts
        self.bind_all("<Control-Return>", lambda e: self._send_message())
        self.bind_all("<Control-n>", lambda e: self._new_chat())
        self.bind_all("<Control-l>", lambda e: self._clear_chat())
        self.bind_all("<Control-comma>", lambda e: self._open_settings())
        self.bind_all("<Escape>", lambda e: self._abort_request())

        # Start queue processor
        self._process_queue()

        # Welcome message
        self._add_system_message("Welcome to Alloy! Select an AI and start chatting.")

    # =========================================================================
    # UI Creation
    # =========================================================================

    def _create_menu(self):
        """Create the menu bar."""
        menubar = tk.Menu(self, bg=COLORS["bg_light"], fg=COLORS["fg"])

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0, bg=COLORS["bg_light"], fg=COLORS["fg"])
        file_menu.add_command(label="New Chat", command=self._new_chat, accelerator="Ctrl+N")
        file_menu.add_command(label="Clear Chat", command=self._clear_chat, accelerator="Ctrl+L")
        file_menu.add_separator()
        file_menu.add_command(label="Reload Config", command=self._reload_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        # View menu
        view_menu = tk.Menu(menubar, tearoff=0, bg=COLORS["bg_light"], fg=COLORS["fg"])
        view_menu.add_command(label="Single View", command=lambda: self._set_view("single"))
        view_menu.add_command(label="Split View", command=lambda: self._set_view("split"))
        view_menu.add_separator()
        view_menu.add_command(label="Settings", command=self._open_settings, accelerator="Ctrl+,")
        menubar.add_cascade(label="View", menu=view_menu)

        # Mode menu
        mode_menu = tk.Menu(menubar, tearoff=0, bg=COLORS["bg_light"], fg=COLORS["fg"])
        mode_menu.add_command(label="Direct Query", command=lambda: self._set_mode("direct"))
        mode_menu.add_separator()
        mode_menu.add_command(label="Roundtable", command=lambda: self._set_mode("roundtable"))
        mode_menu.add_command(label="Chain", command=lambda: self._set_mode("chain"))
        mode_menu.add_command(label="Brainstorm", command=lambda: self._set_mode("brainstorm"))
        mode_menu.add_command(label="Devil's Advocate", command=lambda: self._set_mode("devils-advocate"))
        mode_menu.add_command(label="Roles", command=lambda: self._set_mode("roles"))
        mode_menu.add_command(label="Code Review", command=lambda: self._set_mode("code-review"))
        mode_menu.add_command(label="Solve", command=lambda: self._set_mode("solve"))
        menubar.add_cascade(label="Mode", menu=mode_menu)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0, bg=COLORS["bg_light"], fg=COLORS["fg"])
        help_menu.add_command(label="Available AIs", command=self._show_ais)
        help_menu.add_command(label="Mode Help", command=self._show_mode_help)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config_menu = menubar
        self["menu"] = menubar

    def _create_layout(self):
        """Create the main layout."""
        # Main container
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)

        # Left sidebar
        self.sidebar = self._create_sidebar(main)
        self.sidebar.pack(side="left", fill="y")

        # Separator
        ttk.Separator(main, orient="vertical").pack(side="left", fill="y")

        # Right content area
        content = ttk.Frame(main)
        content.pack(side="left", fill="both", expand=True)

        # Top toolbar
        self._create_toolbar(content)

        # Chat area container (holds either single or split view)
        self.chat_frame = ttk.Frame(content)
        self.chat_frame.pack(fill="both", expand=True, padx=PAD["medium"], pady=PAD["small"])

        # Single view (scrollable message list)
        self.single_view_frame = ttk.Frame(self.chat_frame)
        self.chat_scroll = ScrollableFrame(self.single_view_frame)
        self.chat_scroll.pack(fill="both", expand=True)
        self.message_container = self.chat_scroll.scrollable_frame

        # Split view (grid of AI response panels)
        self.split_view_frame = ttk.Frame(self.chat_frame)
        self.split_panels = {}  # ai_name -> panel widgets

        # Start in single view mode
        self._show_view("single")

        # Input area
        self._create_input_area(content)

    def _create_sidebar(self, parent) -> ttk.Frame:
        """Create the left sidebar with AI selection and history."""
        sidebar = ttk.Frame(parent, width=SIDEBAR_WIDTH)
        sidebar.pack_propagate(False)

        # AI Selection section
        ai_section = ttk.Frame(sidebar)
        ai_section.pack(fill="x", padx=PAD["small"], pady=PAD["medium"])

        ttk.Label(ai_section, text="AI Selection", style="Subheading.TLabel").pack(anchor="w")

        # AI dropdown
        ai_names = list(self.config.get_enabled_ais().keys())
        self.ai_var = tk.StringVar(value=self.current_ai)
        self.ai_dropdown = ttk.Combobox(
            ai_section,
            textvariable=self.ai_var,
            values=ai_names,
            state="readonly",
            width=20
        )
        self.ai_dropdown.pack(fill="x", pady=(PAD["small"], 0))
        self.ai_dropdown.bind("<<ComboboxSelected>>", self._on_ai_selected)

        # Query All button
        ttk.Button(
            ai_section,
            text="Query All AIs",
            command=self._query_all
        ).pack(fill="x", pady=(PAD["small"], 0))

        ttk.Separator(sidebar, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # Mode section
        mode_section = ttk.Frame(sidebar)
        mode_section.pack(fill="x", padx=PAD["small"])

        ttk.Label(mode_section, text="Mode", style="Subheading.TLabel").pack(anchor="w")

        modes = [
            ("Direct", "direct"),
            ("Roundtable", "roundtable"),
            ("Chain", "chain"),
            ("Brainstorm", "brainstorm"),
            ("Devil's Advocate", "devils-advocate"),
            ("Roles", "roles"),
            ("Code Review", "code-review"),
            ("Solve", "solve"),
        ]

        self.mode_var = tk.StringVar(value="direct")
        for label, value in modes:
            rb = ttk.Radiobutton(
                mode_section,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._on_mode_selected
            )
            rb.pack(anchor="w", pady=1)

        # Mode options section (dynamic based on mode)
        self.mode_options_frame = ttk.Frame(sidebar)
        self.mode_options_frame.pack(fill="x", padx=PAD["small"], pady=(PAD["small"], 0))
        self._create_mode_options()

        ttk.Separator(sidebar, orient="horizontal").pack(fill="x", pady=PAD["medium"])

        # History section (placeholder)
        history_section = ttk.Frame(sidebar)
        history_section.pack(fill="both", expand=True, padx=PAD["small"])

        ttk.Label(history_section, text="History", style="Subheading.TLabel").pack(anchor="w")
        ttk.Label(
            history_section,
            text="(Session history\ncoming soon)",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=PAD["small"])

        # New Chat button at bottom
        ttk.Button(
            sidebar,
            text="+ New Chat",
            command=self._new_chat
        ).pack(fill="x", padx=PAD["small"], pady=PAD["medium"])

        return sidebar

    def _create_toolbar(self, parent):
        """Create the toolbar with view toggle and mode options."""
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", padx=PAD["medium"], pady=PAD["small"])

        # View toggle
        view_frame = ttk.Frame(toolbar)
        view_frame.pack(side="left")

        self.view_var = tk.StringVar(value="single")
        ttk.Radiobutton(
            view_frame,
            text="Single View",
            value="single",
            variable=self.view_var,
            command=self._on_view_changed
        ).pack(side="left")
        ttk.Radiobutton(
            view_frame,
            text="Split View",
            value="split",
            variable=self.view_var,
            command=self._on_view_changed
        ).pack(side="left", padx=(PAD["small"], 0))

        # Mode indicator
        self.mode_label = ttk.Label(
            toolbar,
            text="Mode: Direct",
            style="Dim.TLabel"
        )
        self.mode_label.pack(side="right")

    def _create_input_area(self, parent):
        """Create the message input area."""
        input_frame = ttk.Frame(parent)
        input_frame.pack(fill="x", padx=PAD["medium"], pady=PAD["medium"])

        # Input row
        input_row = ttk.Frame(input_frame)
        input_row.pack(fill="x")

        # AI indicator
        self.ai_indicator = ttk.Label(
            input_row,
            text=f"@{self.current_ai}",
            style="Accent.TLabel",
            width=12
        )
        self.ai_indicator.pack(side="left", padx=(0, PAD["small"]))

        # Text input
        self.input_text = tk.Text(
            input_row,
            height=3,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["fg"],
            font=FONTS["body"],
            wrap="word",
            padx=8,
            pady=8
        )
        self.input_text.pack(side="left", fill="x", expand=True)
        self.input_text.bind("<Return>", self._on_enter_key)
        self.input_text.bind("<Shift-Return>", lambda e: None)  # Allow newlines

        # Send button
        self.send_btn = ttk.Button(
            input_row,
            text="Send",
            command=self._send_message,
            style="Accent.TButton",
            width=8
        )
        self.send_btn.pack(side="left", padx=(PAD["small"], 0))

        # Abort button (hidden by default)
        self.abort_btn = ttk.Button(
            input_row,
            text="Stop",
            command=self._abort_request,
            style="Danger.TButton",
            width=8
        )

        # Hint label
        ttk.Label(
            input_frame,
            text="Press Ctrl+Enter to send, Shift+Enter for newline",
            style="Dim.TLabel"
        ).pack(anchor="w", pady=(PAD["xs"], 0))

    def _create_status_bar(self):
        """Create the status bar at the bottom."""
        self.status_bar = ttk.Frame(self)
        self.status_bar.pack(fill="x", side="bottom")

        # Status message
        self.status_label = ttk.Label(
            self.status_bar,
            text="Ready",
            style="Dim.TLabel",
            padding=(PAD["medium"], PAD["xs"])
        )
        self.status_label.pack(side="left")

        # AI count
        ai_count = len(self.config.get_enabled_ais())
        ttk.Label(
            self.status_bar,
            text=f"{ai_count} AIs configured",
            style="Dim.TLabel",
            padding=(PAD["medium"], PAD["xs"])
        ).pack(side="right")

        # Streaming indicator
        self.streaming_label = ttk.Label(
            self.status_bar,
            text="Streaming: ON" if self.config.streaming else "Streaming: OFF",
            style="Dim.TLabel",
            padding=(PAD["medium"], PAD["xs"])
        )
        self.streaming_label.pack(side="right")

    # =========================================================================
    # Event Handlers
    # =========================================================================

    def _on_enter_key(self, event):
        """Handle Enter key in input."""
        # Check if Shift is held
        if event.state & 0x1:  # Shift
            return  # Allow newline
        self._send_message()
        return "break"  # Prevent default newline

    def _on_ai_selected(self, event=None):
        """Handle AI selection change."""
        self.current_ai = self.ai_var.get()
        self.ai_indicator.config(text=f"@{self.current_ai}")

    def _on_mode_selected(self):
        """Handle mode selection change."""
        self.current_mode = self.mode_var.get()
        mode_names = {
            "direct": "Direct",
            "roundtable": "Roundtable",
            "chain": "Chain",
            "brainstorm": "Brainstorm",
            "devils-advocate": "Devil's Advocate",
            "roles": "Roles",
            "code-review": "Code Review",
            "solve": "Solve",
        }
        self.mode_label.config(text=f"Mode: {mode_names.get(self.current_mode, 'Direct')}")
        self._create_mode_options()

    def _create_mode_options(self):
        """Create mode-specific options based on selected mode."""
        # Clear existing options
        for widget in self.mode_options_frame.winfo_children():
            widget.destroy()

        mode = self.mode_var.get()

        # Initialize option variables if not exists
        if not hasattr(self, 'mode_option_vars'):
            self.mode_option_vars = {
                'rounds': tk.IntVar(value=3),
                'judge': tk.StringVar(value=self.config.default_judge),
                'parallel': tk.BooleanVar(value=True),
            }

        # Show relevant options based on mode
        if mode == "direct":
            # No options for direct mode
            return

        ai_names = list(self.config.get_enabled_ais().keys())

        if mode in ("roundtable", "brainstorm"):
            # Rounds option
            row = ttk.Frame(self.mode_options_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text="Rounds:", width=8).pack(side="left")
            ttk.Spinbox(
                row,
                from_=1,
                to=10,
                width=5,
                textvariable=self.mode_option_vars['rounds']
            ).pack(side="left")

        if mode in ("brainstorm", "devils-advocate"):
            # Judge option
            row = ttk.Frame(self.mode_options_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text="Judge:", width=8).pack(side="left")
            judge_combo = ttk.Combobox(
                row,
                textvariable=self.mode_option_vars['judge'],
                values=ai_names,
                state="readonly",
                width=10
            )
            judge_combo.pack(side="left")

        if mode in ("brainstorm", "code-review"):
            # Parallel option
            ttk.Checkbutton(
                self.mode_options_frame,
                text="Run in parallel",
                variable=self.mode_option_vars['parallel']
            ).pack(anchor="w", pady=2)

        if mode == "roles":
            # Template info
            templates = list(ROLE_TEMPLATES.keys())
            ttk.Label(
                self.mode_options_frame,
                text=f"Templates: {', '.join(templates)}",
                style="Dim.TLabel",
                wraplength=150
            ).pack(anchor="w", pady=2)

    def _on_view_changed(self):
        """Handle view mode change."""
        self.view_mode = self.view_var.get()
        self._show_view(self.view_mode)

    def _show_view(self, view: str):
        """Show either single or split view."""
        if view == "single":
            self.split_view_frame.pack_forget()
            self.single_view_frame.pack(fill="both", expand=True)
        else:
            self.single_view_frame.pack_forget()
            self.split_view_frame.pack(fill="both", expand=True)

    def _create_split_panels(self, ai_names: list[str]):
        """Create split view panels for the given AIs."""
        # Clear existing panels
        for widget in self.split_view_frame.winfo_children():
            widget.destroy()
        self.split_panels.clear()

        # Configure grid columns
        num_ais = len(ai_names)
        num_cols = min(3, num_ais)  # Max 3 columns
        num_rows = (num_ais + num_cols - 1) // num_cols

        for i in range(num_cols):
            self.split_view_frame.columnconfigure(i, weight=1, uniform="ai")
        for i in range(num_rows):
            self.split_view_frame.rowconfigure(i, weight=1)

        # Create a panel for each AI
        for idx, ai_name in enumerate(ai_names):
            row = idx // num_cols
            col = idx % num_cols

            # Panel frame
            panel = ttk.Frame(self.split_view_frame, style="Card.TFrame")
            panel.grid(row=row, column=col, sticky="nsew", padx=2, pady=2)

            # Header
            header = ttk.Frame(panel)
            header.pack(fill="x", padx=PAD["small"], pady=(PAD["small"], 0))

            ttk.Label(
                header,
                text=ai_name,
                style="Accent.TLabel",
                font=FONTS["body_bold"]
            ).pack(side="left")

            status_label = ttk.Label(header, text="Waiting...", style="Dim.TLabel")
            status_label.pack(side="right")

            # Content (scrollable text)
            content_frame = ttk.Frame(panel)
            content_frame.pack(fill="both", expand=True, padx=PAD["small"], pady=PAD["small"])

            content_text = tk.Text(
                content_frame,
                wrap="word",
                bg=COLORS["bg_light"],
                fg=COLORS["fg"],
                font=FONTS["body"],
                padx=8,
                pady=8,
                borderwidth=0,
                highlightthickness=0
            )
            content_text.pack(fill="both", expand=True)
            content_text.config(state="disabled")

            # Store references
            self.split_panels[ai_name] = {
                "panel": panel,
                "header": header,
                "status": status_label,
                "text": content_text
            }

    def _update_split_panel(self, ai_name: str, content: str, is_streaming: bool = True):
        """Update content in a split view panel."""
        if ai_name not in self.split_panels:
            return

        panel = self.split_panels[ai_name]
        text_widget = panel["text"]
        status_label = panel["status"]

        # Update status
        status_label.config(text="Streaming..." if is_streaming else "Complete")

        # Update content
        text_widget.config(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", content)
        text_widget.config(state="disabled")
        text_widget.see("end")  # Scroll to bottom

    # =========================================================================
    # Message Handling
    # =========================================================================

    def _send_message(self):
        """Send the current message."""
        if self.is_processing:
            return

        message = self.input_text.get("1.0", "end-1c").strip()
        if not message:
            return

        # Clear input
        self.input_text.delete("1.0", "end")

        # Add user message to display
        self._add_message(Message(role="user", content=message))

        # Process based on mode
        self.is_processing = True
        self.abort_requested = False
        self._update_ui_state()

        if self.current_mode == "direct":
            self._query_ai(self.current_ai, message)
        else:
            self._run_mode(self.current_mode, message)

    def _query_ai(self, ai_name: str, message: str):
        """Query a single AI in a background thread."""
        self._set_status(f"Querying {ai_name}...")

        def worker():
            try:
                if self.config.streaming:
                    # Streaming query
                    stream = self.orchestrator.query_streaming(ai_name, message)
                    content = ""
                    for chunk in stream:
                        if self.abort_requested:
                            break
                        if isinstance(chunk, str):
                            content += chunk
                            self.response_queue.put(("stream_chunk", ai_name, chunk, content))

                    self.response_queue.put(("stream_end", ai_name, content, None))
                else:
                    # Non-streaming query
                    response = self.orchestrator.query(ai_name, message)
                    self.response_queue.put(("response", ai_name, response, None))

            except Exception as e:
                self.response_queue.put(("error", ai_name, str(e), None))

        threading.Thread(target=worker, daemon=True).start()

    def _query_all(self):
        """Query all AIs with the current input."""
        message = self.input_text.get("1.0", "end-1c").strip()
        if not message:
            messagebox.showwarning("No Message", "Please enter a message first.")
            return

        if self.is_processing:
            return

        # Clear input
        self.input_text.delete("1.0", "end")

        # Get all enabled AIs
        ais = list(self.config.get_enabled_ais().keys())

        # Switch to split view and create panels
        self.view_var.set("split")
        self._show_view("split")
        self._create_split_panels(ais)

        # Track pending responses
        self._pending_ais = set(ais)

        self.is_processing = True
        self.abort_requested = False
        self._update_ui_state()
        self._set_status(f"Querying {len(ais)} AIs in parallel...")

        def worker(ai_name):
            try:
                if self.config.streaming:
                    stream = self.orchestrator.query_streaming(ai_name, message)
                    content = ""
                    for chunk in stream:
                        if self.abort_requested:
                            break
                        if isinstance(chunk, str):
                            content += chunk
                            self.response_queue.put(("split_chunk", ai_name, content, None))
                    self.response_queue.put(("split_end", ai_name, content, None))
                else:
                    response = self.orchestrator.query(ai_name, message)
                    if response.success:
                        self.response_queue.put(("split_end", ai_name, response.content, None))
                    else:
                        self.response_queue.put(("split_error", ai_name, response.error, None))
            except Exception as e:
                self.response_queue.put(("split_error", ai_name, str(e), None))

        for ai_name in ais:
            threading.Thread(target=worker, args=(ai_name,), daemon=True).start()

    def _run_mode(self, mode: str, topic: str):
        """Run a collaboration mode in background thread."""
        self._set_status(f"Running {mode} mode...")

        # For now, just do a direct query
        # TODO: Implement full mode execution
        self._add_system_message(f"Mode '{mode}' execution coming soon. Using direct query for now.")
        self._query_ai(self.current_ai, topic)

    def _process_queue(self):
        """Process responses from background threads."""
        try:
            while True:
                msg_type, ai_name, data, extra = self.response_queue.get_nowait()

                if msg_type == "stream_chunk":
                    self._update_streaming_message(ai_name, data)
                elif msg_type == "stream_end":
                    self._finalize_streaming_message(ai_name, data)
                elif msg_type == "response":
                    response: AIResponse = data
                    if response.success:
                        self._add_message(Message(
                            role="ai",
                            ai_name=ai_name,
                            content=response.content
                        ))
                    else:
                        self._add_message(Message(
                            role="error",
                            ai_name=ai_name,
                            content=response.error or "Unknown error"
                        ))
                    self._request_complete()
                elif msg_type == "error":
                    self._add_message(Message(
                        role="error",
                        ai_name=ai_name,
                        content=str(data)
                    ))
                    self._request_complete()
                # Split view message types
                elif msg_type == "split_chunk":
                    self._update_split_panel(ai_name, data, is_streaming=True)
                elif msg_type == "split_end":
                    self._update_split_panel(ai_name, data, is_streaming=False)
                    self._split_complete(ai_name)
                elif msg_type == "split_error":
                    self._update_split_panel(ai_name, f"Error: {data}", is_streaming=False)
                    if ai_name in self.split_panels:
                        self.split_panels[ai_name]["status"].config(
                            text="Error", foreground=COLORS["error"]
                        )
                    self._split_complete(ai_name)

        except queue.Empty:
            pass

        # Schedule next check
        self.after(50, self._process_queue)

    def _split_complete(self, ai_name: str):
        """Called when a split panel AI completes."""
        if hasattr(self, '_pending_ais'):
            self._pending_ais.discard(ai_name)
            remaining = len(self._pending_ais)
            if remaining > 0:
                self._set_status(f"Waiting for {remaining} more AI(s)...")
            else:
                self._request_complete()

    # =========================================================================
    # Message Display
    # =========================================================================

    def _add_message(self, message: Message):
        """Add a message to the chat display."""
        self.messages.append(message)

        # Create message widget
        msg_frame = ttk.Frame(self.message_container, style="Card.TFrame")
        msg_frame.pack(fill="x", pady=PAD["small"], padx=PAD["small"])

        # Header
        header = ttk.Frame(msg_frame)
        header.pack(fill="x", padx=PAD["small"], pady=(PAD["small"], 0))

        if message.role == "user":
            name = "You"
            style = "TLabel"
        elif message.role == "ai":
            name = message.ai_name
            style = "Accent.TLabel"
        elif message.role == "system":
            name = "System"
            style = "Dim.TLabel"
        else:  # error
            name = f"{message.ai_name} (Error)"
            style = "Error.TLabel"

        ttk.Label(header, text=name, style=style, font=FONTS["body_bold"]).pack(side="left")

        timestamp = message.timestamp.strftime("%H:%M")
        ttk.Label(header, text=timestamp, style="Dim.TLabel").pack(side="right")

        # Content
        content_label = tk.Text(
            msg_frame,
            wrap="word",
            bg=COLORS["bg_light"],
            fg=COLORS["fg"],
            font=FONTS["body"],
            padx=8,
            pady=8,
            height=1,
            borderwidth=0,
            highlightthickness=0
        )
        content_label.pack(fill="x", padx=PAD["small"], pady=(PAD["xs"], PAD["small"]))
        content_label.insert("1.0", message.content)
        content_label.config(state="disabled")

        # Auto-resize height
        self._resize_text_widget(content_label)

        # Scroll to bottom
        self.chat_scroll.scroll_to_bottom()

    def _add_system_message(self, text: str):
        """Add a system message."""
        self._add_message(Message(role="system", content=text))

    def _update_streaming_message(self, ai_name: str, content: str):
        """Update or create a streaming message."""
        # Find existing streaming message or create new one
        widget_id = f"streaming_{ai_name}"

        if not hasattr(self, '_streaming_widgets'):
            self._streaming_widgets = {}

        if widget_id not in self._streaming_widgets:
            # Create new streaming message
            msg_frame = ttk.Frame(self.message_container, style="Card.TFrame")
            msg_frame.pack(fill="x", pady=PAD["small"], padx=PAD["small"])

            header = ttk.Frame(msg_frame)
            header.pack(fill="x", padx=PAD["small"], pady=(PAD["small"], 0))

            ttk.Label(
                header,
                text=f"{ai_name} (streaming...)",
                style="Accent.TLabel",
                font=FONTS["body_bold"]
            ).pack(side="left")

            content_text = tk.Text(
                msg_frame,
                wrap="word",
                bg=COLORS["bg_light"],
                fg=COLORS["fg"],
                font=FONTS["body"],
                padx=8,
                pady=8,
                height=1,
                borderwidth=0,
                highlightthickness=0
            )
            content_text.pack(fill="x", padx=PAD["small"], pady=(PAD["xs"], PAD["small"]))

            self._streaming_widgets[widget_id] = {
                "frame": msg_frame,
                "header": header,
                "text": content_text
            }

        # Update content
        widgets = self._streaming_widgets[widget_id]
        text_widget = widgets["text"]
        text_widget.config(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", content)
        text_widget.config(state="disabled")
        self._resize_text_widget(text_widget)
        self.chat_scroll.scroll_to_bottom()

    def _finalize_streaming_message(self, ai_name: str, content: str):
        """Finalize a streaming message."""
        widget_id = f"streaming_{ai_name}"

        if hasattr(self, '_streaming_widgets') and widget_id in self._streaming_widgets:
            widgets = self._streaming_widgets[widget_id]

            # Update header to remove "(streaming...)"
            for child in widgets["header"].winfo_children():
                child.destroy()
            ttk.Label(
                widgets["header"],
                text=ai_name,
                style="Accent.TLabel",
                font=FONTS["body_bold"]
            ).pack(side="left")

            timestamp = datetime.now().strftime("%H:%M")
            ttk.Label(widgets["header"], text=timestamp, style="Dim.TLabel").pack(side="right")

            # Update content one final time
            text_widget = widgets["text"]
            text_widget.config(state="normal")
            text_widget.delete("1.0", "end")
            text_widget.insert("1.0", content)
            text_widget.config(state="disabled")
            self._resize_text_widget(text_widget)

            # Add to messages list
            self.messages.append(Message(role="ai", ai_name=ai_name, content=content))

            # Clean up
            del self._streaming_widgets[widget_id]

        self._request_complete()

    def _resize_text_widget(self, widget: tk.Text):
        """Resize a text widget to fit its content."""
        widget.update_idletasks()
        # Count lines
        content = widget.get("1.0", "end-1c")
        lines = content.count('\n') + 1
        # Estimate wrapped lines based on widget width
        widget_width = widget.winfo_width()
        if widget_width > 0:
            chars_per_line = widget_width // 8  # Rough estimate
            if chars_per_line > 0:
                for line in content.split('\n'):
                    lines += len(line) // chars_per_line

        # Cap at reasonable height
        height = min(max(lines, 1), 30)
        widget.config(height=height)

    def _request_complete(self):
        """Called when a request completes."""
        # Check if all streaming is done
        if hasattr(self, '_streaming_widgets') and self._streaming_widgets:
            return  # Still streaming

        self.is_processing = False
        self._update_ui_state()
        self._set_status("Ready")

    # =========================================================================
    # UI State
    # =========================================================================

    def _update_ui_state(self):
        """Update UI based on current state."""
        if self.is_processing:
            self.send_btn.pack_forget()
            self.abort_btn.pack(side="left", padx=(PAD["small"], 0))
            self.input_text.config(state="disabled")
        else:
            self.abort_btn.pack_forget()
            self.send_btn.pack(side="left", padx=(PAD["small"], 0))
            self.input_text.config(state="normal")
            self.input_text.focus_set()

    def _set_status(self, text: str):
        """Update status bar text."""
        self.status_label.config(text=text)

    def _set_view(self, view: str):
        """Set the view mode."""
        self.view_var.set(view)
        self.view_mode = view

    def _set_mode(self, mode: str):
        """Set the collaboration mode."""
        self.mode_var.set(mode)
        self._on_mode_selected()

    def _abort_request(self):
        """Abort the current request."""
        if self.is_processing:
            self.abort_requested = True
            self._set_status("Aborting...")

    # =========================================================================
    # Commands
    # =========================================================================

    def _new_chat(self):
        """Start a new chat."""
        self.messages.clear()
        if hasattr(self, '_streaming_widgets'):
            self._streaming_widgets.clear()

        # Clear message container
        for child in self.message_container.winfo_children():
            child.destroy()

        self._add_system_message("New conversation started.")

    def _clear_chat(self):
        """Clear the chat display."""
        self._new_chat()

    def _reload_config(self):
        """Reload configuration."""
        try:
            self.config = Config.load(self.config_path)
            self.orchestrator = Orchestrator(self.config)
            self.router = Router(self.config)

            # Update AI dropdown
            ai_names = list(self.config.get_enabled_ais().keys())
            self.ai_dropdown.config(values=ai_names)
            if self.current_ai not in ai_names and ai_names:
                self.ai_var.set(ai_names[0])
                self._on_ai_selected()

            self._add_system_message("Configuration reloaded.")
        except Exception as e:
            messagebox.showerror("Reload Error", f"Failed to reload config:\n{e}")

    def _open_settings(self):
        """Open the settings editor."""
        SettingsEditor(self, config_path=self.config_path)

    def _show_ais(self):
        """Show available AIs."""
        ais = self.config.get_enabled_ais()
        lines = ["Available AIs:\n"]
        for name, ai in ais.items():
            lines.append(f"  {name}: {ai.description or 'No description'}")
        self._add_system_message("\n".join(lines))

    def _show_mode_help(self):
        """Show mode help."""
        help_text = """Collaboration Modes:

  Direct: Query a single AI directly
  Roundtable: Each AI discusses the topic, seeing previous responses
  Chain: AIs refine the response sequentially
  Brainstorm: All AIs generate ideas in parallel
  Devil's Advocate: Steelman, attack, and verdict phases
  Roles: Assign specific roles to each AI
  Code Review: Multi-perspective code review
  Solve: Collaborative problem solving"""
        self._add_system_message(help_text)

    def _show_about(self):
        """Show about dialog."""
        messagebox.showinfo(
            "About Alloy",
            "Alloy - Multiple AIs, stronger together\n\n"
            "A multi-AI collaboration tool that orchestrates\n"
            "Claude, Gemini, Copilot, and other AI assistants\n"
            "to work together on complex tasks."
        )


def run_gui(config_path: Optional[Path] = None):
    """Run the Alloy GUI application."""
    app = AlloyGUI(config_path)
    app.mainloop()


if __name__ == "__main__":
    run_gui()
