"""Styling constants for Alloy GUI."""

import platform

# Color scheme - dark theme to match CLI aesthetic
COLORS = {
    "bg": "#1e1e1e",           # Dark background
    "bg_light": "#2d2d2d",     # Slightly lighter background
    "bg_input": "#3d3d3d",     # Input field background
    "fg": "#ffffff",           # White text
    "fg_dim": "#888888",       # Dimmed text
    "accent": "#00bcd4",       # Cyan accent (matches Claude color)
    "accent_hover": "#00a5bb", # Darker accent for hover
    "accent_light": "#33c9dc", # Lighter accent for focus
    "success": "#4caf50",      # Green for success
    "warning": "#ff9800",      # Orange for warnings
    "error": "#f44336",        # Red for errors
    "border": "#555555",       # Border color
    "focus": "#00bcd4",        # Focus ring color
    "card_border": "#404040",  # Card border color
}

# AI-specific colors (matching main app)
AI_COLORS = {
    "claude": "#00bcd4",    # Cyan
    "gemini": "#4285f4",    # Blue
    "codex": "#4caf50",     # Green
    "copilot": "#e91e63",   # Magenta/Pink
}

# Available colors for AI color picker
COLOR_OPTIONS = [
    ("Cyan", "cyan"),
    ("Blue", "blue"),
    ("Green", "green"),
    ("Magenta", "magenta"),
    ("Yellow", "yellow"),
    ("Red", "red"),
    ("White", "white"),
]

# Cross-platform font handling
def _get_platform_fonts():
    """Get platform-appropriate fonts."""
    system = platform.system()

    if system == "Darwin":  # macOS
        return {
            "heading": ("SF Pro Display", 18, "bold"),
            "subheading": ("SF Pro Display", 14, "bold"),
            "body": ("SF Pro Text", 11),
            "body_bold": ("SF Pro Text", 11, "bold"),
            "small": ("SF Pro Text", 9),
            "mono": ("SF Mono", 10),
        }
    elif system == "Linux":
        return {
            "heading": ("Ubuntu", 18, "bold"),
            "subheading": ("Ubuntu", 14, "bold"),
            "body": ("Ubuntu", 11),
            "body_bold": ("Ubuntu", 11, "bold"),
            "small": ("Ubuntu", 9),
            "mono": ("Ubuntu Mono", 10),
        }
    else:  # Windows and fallback
        return {
            "heading": ("Segoe UI", 18, "bold"),
            "subheading": ("Segoe UI", 14, "bold"),
            "body": ("Segoe UI", 11),
            "body_bold": ("Segoe UI", 11, "bold"),
            "small": ("Segoe UI", 9),
            "mono": ("Consolas", 10),
        }

FONTS = _get_platform_fonts()

# Padding
PAD = {
    "xs": 2,
    "small": 5,
    "medium": 10,
    "large": 20,
    "xl": 30,
}

# Window sizes
WINDOW_SIZES = {
    "wizard": (600, 500),
    "settings": (700, 550),
}


def apply_dark_theme(root):
    """Apply dark theme to ttk widgets with focus indicators and hover states."""
    import tkinter.ttk as ttk

    style = ttk.Style()

    # Try to use a theme that supports customization
    try:
        style.theme_use("clam")
    except Exception:
        pass

    # Configure base colors
    style.configure(".",
        background=COLORS["bg"],
        foreground=COLORS["fg"],
        fieldbackground=COLORS["bg_input"],
        font=FONTS["body"],
        focuscolor=COLORS["focus"],
        borderwidth=1
    )

    # Frame styles
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Light.TFrame", background=COLORS["bg_light"])

    # Card frame - with visible border for visual grouping
    style.configure("Card.TFrame",
        background=COLORS["bg_light"],
        borderwidth=1,
        relief="solid"
    )

    # Label styles
    style.configure("TLabel",
        background=COLORS["bg"],
        foreground=COLORS["fg"]
    )
    style.configure("Heading.TLabel",
        font=FONTS["heading"],
        background=COLORS["bg"],
        foreground=COLORS["fg"]
    )
    style.configure("Subheading.TLabel",
        font=FONTS["subheading"],
        background=COLORS["bg"],
        foreground=COLORS["fg"]
    )
    style.configure("Dim.TLabel",
        background=COLORS["bg"],
        foreground=COLORS["fg_dim"]
    )
    style.configure("Success.TLabel",
        background=COLORS["bg"],
        foreground=COLORS["success"]
    )
    style.configure("Error.TLabel",
        background=COLORS["bg"],
        foreground=COLORS["error"]
    )
    style.configure("Warning.TLabel",
        background=COLORS["bg"],
        foreground=COLORS["warning"]
    )
    style.configure("Accent.TLabel",
        background=COLORS["bg"],
        foreground=COLORS["accent"]
    )

    # Button with hover and focus states
    style.configure("TButton",
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        padding=(PAD["medium"], PAD["small"]),
        borderwidth=1
    )
    style.map("TButton",
        background=[
            ("pressed", COLORS["accent_hover"]),
            ("active", COLORS["accent"]),
            ("focus", COLORS["bg_light"]),
        ],
        foreground=[
            ("disabled", COLORS["fg_dim"]),
        ],
        bordercolor=[
            ("focus", COLORS["focus"]),
        ],
        relief=[
            ("pressed", "sunken"),
            ("!pressed", "raised"),
        ]
    )

    # Accent button (primary action)
    style.configure("Accent.TButton",
        background=COLORS["accent"],
        foreground=COLORS["fg"]
    )
    style.map("Accent.TButton",
        background=[
            ("pressed", COLORS["accent"]),
            ("active", COLORS["accent_hover"]),
        ]
    )

    # Secondary button (less prominent)
    style.configure("Secondary.TButton",
        background=COLORS["bg_light"],
        foreground=COLORS["fg_dim"]
    )

    # Danger button (destructive actions)
    style.configure("Danger.TButton",
        background=COLORS["error"],
        foreground=COLORS["fg"]
    )

    # Entry with focus highlight
    style.configure("TEntry",
        fieldbackground=COLORS["bg_input"],
        foreground=COLORS["fg"],
        insertcolor=COLORS["fg"],
        borderwidth=1,
        padding=5
    )
    style.map("TEntry",
        fieldbackground=[
            ("focus", COLORS["bg_input"]),
            ("readonly", COLORS["bg_light"]),
        ],
        bordercolor=[
            ("focus", COLORS["focus"]),
        ]
    )

    # Checkbutton with hover
    style.configure("TCheckbutton",
        background=COLORS["bg"],
        foreground=COLORS["fg"]
    )
    style.map("TCheckbutton",
        background=[
            ("active", COLORS["bg"]),
        ],
        foreground=[
            ("disabled", COLORS["fg_dim"]),
            ("active", COLORS["accent_light"]),
        ]
    )

    # Combobox with focus
    style.configure("TCombobox",
        fieldbackground=COLORS["bg_input"],
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        arrowcolor=COLORS["fg"],
        borderwidth=1,
        padding=5
    )
    style.map("TCombobox",
        fieldbackground=[
            ("readonly", COLORS["bg_input"]),
            ("focus", COLORS["bg_input"]),
        ],
        bordercolor=[
            ("focus", COLORS["focus"]),
        ],
        arrowcolor=[
            ("disabled", COLORS["fg_dim"]),
        ]
    )

    # Notebook (tabs) with focus
    style.configure("TNotebook",
        background=COLORS["bg"],
        borderwidth=0
    )
    style.configure("TNotebook.Tab",
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        padding=(PAD["medium"], PAD["small"])
    )
    style.map("TNotebook.Tab",
        background=[
            ("selected", COLORS["bg"]),
            ("active", COLORS["accent_hover"]),
        ],
        foreground=[
            ("selected", COLORS["accent"]),
        ]
    )

    # Spinbox with focus
    style.configure("TSpinbox",
        fieldbackground=COLORS["bg_input"],
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        arrowcolor=COLORS["fg"],
        borderwidth=1,
        padding=5
    )
    style.map("TSpinbox",
        fieldbackground=[
            ("focus", COLORS["bg_input"]),
        ],
        bordercolor=[
            ("focus", COLORS["focus"]),
        ],
        arrowcolor=[
            ("disabled", COLORS["fg_dim"]),
        ]
    )

    # Separator
    style.configure("TSeparator", background=COLORS["border"])

    # Progressbar
    style.configure("TProgressbar",
        background=COLORS["accent"],
        troughcolor=COLORS["bg_light"],
        borderwidth=0
    )

    # Scrollbar with hover
    style.configure("TScrollbar",
        background=COLORS["bg_light"],
        troughcolor=COLORS["bg"],
        borderwidth=0,
        arrowcolor=COLORS["fg"]
    )
    style.map("TScrollbar",
        background=[
            ("active", COLORS["accent"]),
        ]
    )

    # Labelframe (used for grouping)
    style.configure("TLabelframe",
        background=COLORS["bg"],
        bordercolor=COLORS["border"]
    )
    style.configure("TLabelframe.Label",
        background=COLORS["bg"],
        foreground=COLORS["fg"],
        font=FONTS["body_bold"]
    )

    return style
