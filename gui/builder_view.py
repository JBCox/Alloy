"""Collaborative Model Builder view (spec R-91, R-92, addendum R-110; design Section 9.2).

``BuilderWindow`` is a ``tk.Toplevel`` bound to a ``builder.viewmodel.BuilderSession``. The session runs the
engine on its own worker thread and publishes events and snapshots on a ``queue.Queue``; this window drains the
queue with ``after`` (the same pattern as ``gui/app.py``) and never calls the engine, a provider, or Blender on
the Tk thread. Every control submits a job; every panel renders a snapshot.

Panels (R-91): project and reference intake; agents with capability tiers and the effective reasoning setting;
stage, task, ownership, and active operations; part tree with construction relations; reference and render
comparison with zoom and view or component selection; before and after; toggleable measurements; observed and
inferred labels; findings linked to parts and images; review coverage; consumption and configured limits; controls
for correction, acceptance, pause and resume, checkpoints, attended and unattended runs. The concept approval
panel (R-110): open requests with copyable prompts, candidates beside the anchor, both seats' verdicts, approve,
reject, regenerate, with the approval mode always visible (D11).

Previews are the actual artifacts labelled with revision and render ids (R-92). They are scaled to fit for
presentation only; the files are never modified and the label says so. A generated image is labelled a
hypothesis, never evidence of the original design (R-32).

Look: the chat app's theme (``gui/styles.py``): card panels, subheadings, dim captions, accent and danger buttons,
tooltips for the explanations.
"""
from __future__ import annotations

import getpass
import queue
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

from PIL import Image, ImageTk

from builder.config import DEFAULT_LIMITS, BuilderConfig
from builder.viewmodel import BuilderSession, overlay_rects

from .styles import COLORS, FONTS, PAD, apply_dark_theme
from .tooltips import ToolTip

POLL_MS = 50
WINDOW_SIZE = (1560, 940)
PANE_SIZE = (430, 320)
CONCEPT_PANE_SIZE = (320, 240)          # the anchor/candidate pair: wide enough to compare silhouettes
SASH_FRACTIONS = {"default": (0.28, 0.70), "concept": (0.16, 0.38)}   # left | centre | right column splits
LIGHT = "Light.TFrame"
ATTENDED_HELP = ("Attended (default): whole-model planning and blockout come first, then the run stops for your acceptance "
                 "before advancing past a detailed component (R-76). Unattended: advances within the configured limits and "
                 "leaves every component unaccepted for your review. 'Ready for user review' is never 'accepted'.")
LIVE_PREFLIGHT_WARNING = ("A live preflight makes real provider invocations for every configured agent (image probe, write "
                          "probe, session probe, cancellation probe; up to one repair round each) and SPENDS provider usage. "
                          "Claude probes have cost about 0.5 to 0.8 USD each by the CLI's own estimate; codex reports no cost "
                          "(unknown, never zero).\n\nRun the live preflight now?")
IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")]


# ============================================================================================ widgets

def _install_styles(widget: tk.Misc) -> None:
    """Builder-only styles on top of the shared theme (the chat app uses no Treeview)."""
    style = ttk.Style(widget)
    style.configure("Treeview", background=COLORS["bg_light"], fieldbackground=COLORS["bg_light"], foreground=COLORS["fg"],
                    font=FONTS["body"], rowheight=24, borderwidth=0, relief="flat")
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview.Heading", background=COLORS["bg"], foreground=COLORS["fg_dim"], font=FONTS["small"],
                    relief="flat", borderwidth=0, padding=(PAD["small"], PAD["xs"]))
    style.map("Treeview.Heading", background=[("active", COLORS["bg"])])
    style.map("Treeview", background=[("selected", COLORS["accent"])], foreground=[("selected", COLORS["bg"])])
    style.configure("Builder.Card.TLabel", background=COLORS["bg_light"], foreground=COLORS["fg"], font=FONTS["body"])
    style.configure("Builder.CardTitle.TLabel", background=COLORS["bg_light"], foreground=COLORS["fg"], font=FONTS["body_bold"])
    style.configure("Builder.CardDim.TLabel", background=COLORS["bg_light"], foreground=COLORS["fg_dim"], font=FONTS["small"])
    style.configure("Builder.CardAccent.TLabel", background=COLORS["bg_light"], foreground=COLORS["accent"], font=FONTS["body"])
    style.configure("Builder.Card.TCheckbutton", background=COLORS["bg_light"], foreground=COLORS["fg"])
    style.map("Builder.Card.TCheckbutton", background=[("active", COLORS["bg_light"])])
    style.configure("Builder.Card.TRadiobutton", background=COLORS["bg_light"], foreground=COLORS["fg"])
    style.map("Builder.Card.TRadiobutton", background=[("active", COLORS["bg_light"])])
    style.configure("Builder.TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("Builder.Status.TLabel", background=COLORS["bg_light"], foreground=COLORS["accent"], font=FONTS["body"])


def _card(parent: tk.Misc, title: str | None = None, *, help_text: str | None = None) -> ttk.Frame:
    """A panel in the chat app's card style; children use the ``Builder.Card*`` label styles or ``Light.TFrame``."""
    card = ttk.Frame(parent, style="Card.TFrame", padding=(PAD["medium"], PAD["small"]))
    if title:
        head = ttk.Frame(card, style=LIGHT)
        head.pack(fill="x", pady=(0, PAD["small"]))
        label = ttk.Label(head, text=title, style="Builder.CardTitle.TLabel")
        label.pack(side="left")
        if help_text:
            hint = ttk.Label(head, text="?", style="Builder.CardDim.TLabel")
            hint.pack(side="right")
            ToolTip(hint, help_text)
    return card


def _text(parent: tk.Misc, *, height: int, wrap: str = "word") -> tk.Text:
    """A flat read-only text panel that reads as part of the card, not a sunken box."""
    widget = tk.Text(parent, height=height, wrap=wrap, bg=COLORS["bg_light"], fg=COLORS["fg"], font=FONTS["body"],
                     relief="flat", bd=0, highlightthickness=0, padx=PAD["xs"], pady=PAD["xs"], insertbackground=COLORS["fg"],
                     selectbackground=COLORS["accent"], cursor="arrow")
    widget.configure(state="disabled")
    return widget


def _set_text(widget: tk.Text, text: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")


def _tree(parent: tk.Misc, columns: list[tuple[str, str, int]], *, show: str = "headings", height: int = 8) -> ttk.Treeview:
    tree = ttk.Treeview(parent, columns=[c[0] for c in columns], show=show, height=height)
    for key, heading, width in columns:
        tree.heading(key, text=heading, anchor="w")
        tree.column(key, width=width, minwidth=40, stretch=True, anchor="w")
    return tree


def _scrolled(parent: tk.Misc, widget_factory: Callable[[tk.Misc], Any]) -> Any:
    frame = ttk.Frame(parent, style=LIGHT)
    widget = widget_factory(frame)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    widget.configure(yscrollcommand=vsb.set)
    widget.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    widget.frame = frame  # type: ignore[attr-defined]
    return widget


def _scroll_frame(parent: tk.Misc) -> tuple[ttk.Frame, ttk.Frame]:
    """A vertically scrollable container: returns (outer, inner). Cards pack into ``inner``; the outer frame is what the
    caller packs. Used by tabs whose stacked cards can exceed the window height (the Run tab with its Limits card)."""
    outer = ttk.Frame(parent)
    canvas = tk.Canvas(outer, bg=COLORS["bg"], highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    outer.rowconfigure(0, weight=1)
    outer.columnconfigure(0, weight=1)
    inner = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _inner_configured(event: Any = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _canvas_configured(event: Any) -> None:
        canvas.itemconfigure(window, width=event.width)

    def _wheel(event: Any) -> None:
        canvas.yview_scroll(-int(event.delta / 120), "units")

    inner.bind("<Configure>", _inner_configured)
    canvas.bind("<Configure>", _canvas_configured)
    for w in (canvas, inner):
        w.bind("<MouseWheel>", _wheel)
    outer.canvas = canvas  # type: ignore[attr-defined]
    return outer, inner


def _caption(parent: tk.Misc, textvariable: tk.StringVar, *, wraplength: int = 400) -> ttk.Label:
    return ttk.Label(parent, textvariable=textvariable, style="Builder.CardDim.TLabel", wraplength=wraplength, justify="left")


def _labelled_entry(parent: tk.Misc, label: str, var: tk.StringVar, *, width: int = 18) -> ttk.Frame:
    box = ttk.Frame(parent, style=LIGHT)
    ttk.Label(box, text=label, style="Builder.CardDim.TLabel").pack(anchor="w")
    ttk.Entry(box, textvariable=var, width=width).pack(fill="x")
    return box


# ============================================================================================ image panes

class ImagePane(ttk.Frame):
    """One preview of an actual artifact. The label carries the ids; the presentation line says how the image is
    scaled on screen. Measurements are drawn only from a measurement of the render's revision (R-68)."""

    def __init__(self, parent: tk.Misc, title: str, *, size: tuple[int, int] = PANE_SIZE, compact: bool = False):
        super().__init__(parent, style="Card.TFrame", padding=(PAD["small"], PAD["small"]))
        self.compact = compact           # one-line label (ids only); the full provenance is shown elsewhere
        self.title_var = tk.StringVar(value=title)
        self.label_var = tk.StringVar(value="nothing selected")
        self.presentation_var = tk.StringVar(value="")
        self.snapshot: dict[str, Any] = {}
        self.current_render_id: str | None = None
        self.current_reference_id: str | None = None
        self.zoom = 1.0
        self.measure_on = False
        self._image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self.image_size: tuple[int, int] = (0, 0)
        self.source_size: tuple[int, int] = (0, 0)
        self._scale = 1.0
        self._origin = (0, 0)
        # target-region selection by dragging (R-26, R-27): armed for one drag; the result is in ORIGINAL pixels
        self.region_mode = False
        self.on_region: Callable[[list[int]], None] | None = None
        self._regions: list[dict[str, Any]] = []
        self._drag_start: tuple[int, int] | None = None
        ttk.Label(self, textvariable=self.title_var, style="Builder.CardTitle.TLabel").pack(anchor="w")
        self._label = ttk.Label(self, textvariable=self.label_var, style="Builder.Card.TLabel", wraplength=size[0], justify="left")
        self._label.pack(anchor="w", pady=(0, PAD["xs"]))
        self.canvas = tk.Canvas(self, width=size[0], height=size[1], bg=COLORS["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._last_box: tuple[int, int] | None = None
        self._last_width: int | None = None
        self.canvas.bind("<Configure>", self._canvas_configured)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        # The caption sits in a strip of fixed height: its text depends on the scale, the scale on the canvas size,
        # and the canvas size must therefore never depend on the caption's line count (a Tk geometry loop otherwise).
        strip = ttk.Frame(self, style=LIGHT, height=(2 if compact else 4) * (FONTS["small"][1] + 6))
        strip.pack(fill="x", pady=(PAD["xs"], 0))
        strip.pack_propagate(False)
        self._presentation = _caption(strip, self.presentation_var, wraplength=size[0])
        self._presentation.pack(anchor="nw")
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, event: Any = None) -> None:
        width = max(120, self.winfo_width() - 2 * PAD["small"] - 4)
        if width == self._last_width:
            return
        self._last_width = width
        self._label.configure(wraplength=width)
        self._presentation.configure(wraplength=width)

    def _canvas_configured(self, event: Any) -> None:
        box = (int(event.width), int(event.height))
        if box == self._last_box:
            return
        self._last_box = box
        self._redraw()

    # --- selection -------------------------------------------------------------------------------------------

    def bind_snapshot(self, snap: dict[str, Any]) -> None:
        self.snapshot = snap
        if self.current_render_id:
            if any(r["id"] == self.current_render_id for r in snap.get("renders", [])):
                self.show_render(self.current_render_id)
            else:
                self.clear()
        elif self.current_reference_id:
            if any(r["id"] == self.current_reference_id for r in snap.get("references", [])):
                self.show_reference(self.current_reference_id)
            else:
                self.clear()

    def clear(self) -> None:
        self.current_render_id = self.current_reference_id = None
        self._regions = []
        self._image = self._photo = None
        self.image_size = self.source_size = (0, 0)
        self.label_var.set("nothing selected")
        self.presentation_var.set("")
        self.canvas.delete("all")

    def show_render(self, render_id: str) -> None:
        render = next((r for r in self.snapshot.get("renders", []) if r["id"] == render_id), None)
        if render is None:
            self.clear()
            return
        self.current_render_id, self.current_reference_id = render_id, None
        self._regions = []
        label = (f"render {render['id']}  ·  revision {render['revision_id']}  ·  view {render['view_name']} ({render['mode']})  ·  "
                 f"evidence: {render['evidence_label']}")
        if render.get("part_id"):
            label += f"  ·  part {render['part_id']}"
        if render.get("state") != "ok":
            label += f"  ·  FAILED: {render.get('error')}"
        elif render.get("stale"):
            label += f"  ·  STALE ({render.get('stale_reason')}): not current evidence (R-64)"
        self.label_var.set(label)
        self._load(render.get("file"))

    def show_reference(self, reference_id: str) -> None:
        ref = next((r for r in self.snapshot.get("references", []) if r["id"] == reference_id), None)
        concept = self.snapshot.get("concept") or {}
        if ref is None and (concept.get("anchor") or {}).get("id") == reference_id:
            ref = concept["anchor"]
        if ref is None:
            ref = next((c for c in concept.get("candidates", []) if c["id"] == reference_id), None)
        if ref is None:
            self.clear()
            return
        self.current_reference_id, self.current_render_id = reference_id, None
        self._regions = list(ref.get("regions") or [])
        self.label_var.set(reference_label(ref))
        self._load(ref.get("file"))

    def show_reference_entry(self, ref: dict[str, Any]) -> None:
        """Show a reference dict directly (concept candidates and anchors)."""
        self.current_reference_id, self.current_render_id = ref["id"], None
        self._regions = list(ref.get("regions") or [])
        self.label_var.set(reference_label(ref, compact=self.compact))
        self._load(ref.get("file"))

    # --- drawing ---------------------------------------------------------------------------------------------

    def _load(self, path: str | None) -> None:
        self._image = None
        if path and Path(path).is_file():
            try:
                with Image.open(path) as im:
                    self._image = im.convert("RGB")
                self.source_size = self._image.size
            except OSError as exc:
                self.presentation_var.set(f"unreadable image: {exc}")
        else:
            self.presentation_var.set("missing file: this artifact cannot be shown (R-64)")
        self._redraw()

    def _box(self) -> tuple[int, int]:
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            w, h = int(self.canvas["width"]), int(self.canvas["height"])
        return max(w, 1), max(h, 1)

    def _redraw(self) -> None:
        self.canvas.delete("all")
        if self._image is None:
            self._photo = None
            self.image_size = (0, 0)
            return
        bw, bh = self._box()
        w, h = self._image.size
        fit = min(bw / w, bh / h)
        self._scale = fit * self.zoom
        dw, dh = max(1, round(w * self._scale)), max(1, round(h * self._scale))
        shown = self._image if (dw, dh) == (w, h) else self._image.resize((dw, dh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(shown)
        self.image_size = (dw, dh)
        ox = max(0, (bw - dw) // 2)
        oy = max(0, (bh - dh) // 2)
        self._origin = (ox, oy)
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, max(bw, dw), max(bh, dh)))
        pct = round(self._scale * 100)
        self.presentation_var.set(f"presentation: scaled to {pct}% to fit (zoom x{self.zoom:g}), aspect {w}:{h} preserved; "
                                  f"the evidence file is unmodified")
        if self.measure_on:
            self._draw_measurements()
        self._draw_regions()

    # --- target regions (R-26, R-27) ------------------------------------------------------------------------

    def canvas_to_original(self, x0: float, y0: float, x1: float, y1: float) -> list[int] | None:
        """A canvas rectangle as ``[x, y, w, h]`` in the ORIGINAL image's pixels: the presentation scale and the
        centring offset are undone and the result is clamped to the image. None when nothing is shown or empty."""
        if self._image is None or self._scale <= 0:
            return None
        w, h = self._image.size
        ox, oy = self._origin
        ax, bx = sorted((x0, x1))
        ay, by = sorted((y0, y1))
        left = min(max(round((ax - ox) / self._scale), 0), w)
        right = min(max(round((bx - ox) / self._scale), 0), w)
        top = min(max(round((ay - oy) / self._scale), 0), h)
        bottom = min(max(round((by - oy) / self._scale), 0), h)
        if right - left <= 0 or bottom - top <= 0:
            return None
        return [int(left), int(top), int(right - left), int(bottom - top)]

    def _drag_begin(self, event: Any) -> None:
        if not self.region_mode or self._image is None:
            return
        self._drag_start = (int(event.x), int(event.y))
        self.canvas.delete("rubber")

    def _drag_move(self, event: Any) -> None:
        if not self.region_mode or self._drag_start is None:
            return
        self.canvas.delete("rubber")
        x0, y0 = self._drag_start
        self.canvas.create_rectangle(x0, y0, int(event.x), int(event.y), outline=COLORS["accent"], width=2, dash=(4, 2), tags="rubber")

    def _drag_end(self, event: Any) -> None:
        if not self.region_mode or self._drag_start is None:
            return
        x0, y0 = self._drag_start
        self._drag_start = None
        self.canvas.delete("rubber")
        bbox = self.canvas_to_original(x0, y0, int(event.x), int(event.y))
        self.region_mode = False           # one region per arming
        if bbox is not None and self.on_region is not None:
            self.on_region(bbox)

    def _draw_regions(self) -> None:
        self.canvas.delete("region")
        if self._image is None or not self._regions:
            return
        ox, oy = self._origin
        for g in self._regions:
            try:
                x, y, w, h = (float(v) for v in g.get("bbox") or [])
            except (TypeError, ValueError):
                continue
            x0, y0 = ox + x * self._scale, oy + y * self._scale
            self.canvas.create_rectangle(x0, y0, x0 + w * self._scale, y0 + h * self._scale, outline=COLORS["accent"], width=2,
                                         tags="region")
            self.canvas.create_text(x0 + 3, y0 + 2, anchor="nw", text=f"{g.get('name')} ({g.get('purpose')})", fill=COLORS["accent"],
                                    tags="region", font=FONTS["small"])

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.1, float(zoom))
        self._redraw()

    def set_measurements(self, on: bool) -> None:
        self.measure_on = bool(on)
        self._redraw()

    def _draw_measurements(self) -> None:
        self.canvas.delete("measure")
        if not self.current_render_id:
            return
        rects = overlay_rects(self.snapshot, self.current_render_id)
        if not rects:
            self.presentation_var.set(self.presentation_var.get() + " · measurements: no measurement recorded for this revision")
            return
        ox, oy = self._origin
        for r in rects:
            x0, y0 = ox + r["x"] * self._scale, oy + r["y"] * self._scale
            x1, y1 = x0 + r["w"] * self._scale, y0 + r["h"] * self._scale
            self.canvas.create_rectangle(x0, y0, x1, y1, outline=COLORS["warning"], width=2, tags="measure")
            self.canvas.create_text(x0 + 3, y0 + 2, anchor="nw", text=r["part_id"], fill=COLORS["warning"], tags="measure",
                                    font=FONTS["small"])
        self.presentation_var.set(self.presentation_var.get() + f" · measurements: {len(rects)} measured box(es) projected from "
                                  f"revision {rects[0]['revision_id']} (bounding boxes, not calibrated to the references)")


def reference_label(ref: dict[str, Any], *, compact: bool = False) -> str:
    labels = ",".join(ref.get("labels") or [])
    if compact:
        kind = "hypothesis" if not ref.get("evidence_of_original") else "evidence of original"
        return f"{ref['id']}  ·  [{labels}]  ·  {ref.get('canon_state')}  ·  {kind}"
    evidence = "yes" if ref.get("evidence_of_original") else "no (generated: a hypothesis, never evidence of the original, R-32)"
    text = (f"reference {ref['id']}  ·  v{ref.get('version', 1)}  ·  [{labels}]  ·  {ref.get('canon_state')} "
            f"({ref.get('precedence_label')})  ·  evidence of original: {evidence}")
    d = ref.get("declared") or {}
    if d:
        text += f"  ·  declared by owner: vendor={d.get('vendor') or '-'} model={d.get('model') or '-'} (not verified)"
    if ref.get("composite"):
        text += "  ·  composite sheet: target region required (R-27)"
    regions = ref.get("regions") or []
    if regions and not compact:
        text += "  ·  regions (original pixels): " + "; ".join(f"{g.get('name')} {g.get('bbox')} [{g.get('purpose')}]" for g in regions)
    return text


# ============================================================================================ comparison

class ComparisonPanel(ttk.Frame):
    """Reference beside render, or a finding's before beside its after. Zoom and measurements apply to both."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.snapshot: dict[str, Any] = {}
        self.mode_var = tk.StringVar(value="reference/render")
        bar = _card(self)
        bar.pack(fill="x", pady=(0, PAD["small"]))
        row0 = ttk.Frame(bar, style=LIGHT)
        row0.pack(fill="x")
        ttk.Label(row0, text="Compare", style="Builder.CardTitle.TLabel").pack(side="left", padx=(0, PAD["medium"]))
        ttk.Radiobutton(row0, text="reference / render", variable=self.mode_var, value="reference/render",
                        command=self._mode_changed, style="Builder.Card.TRadiobutton").pack(side="left", padx=(0, PAD["small"]))
        ttk.Radiobutton(row0, text="before / after (finding)", variable=self.mode_var, value="before/after",
                        command=self._mode_changed, style="Builder.Card.TRadiobutton").pack(side="left")
        self.measure_var = tk.BooleanVar(value=False)
        measure = ttk.Checkbutton(row0, text="measured boxes", variable=self.measure_var, command=self._measure_changed,
                                  style="Builder.Card.TCheckbutton")
        measure.pack(side="right")
        ToolTip(measure, "Overlay the bounding boxes measured for the render's revision, projected through the recorded camera. "
                         "Bounding boxes only, not calibrated to the references (R-68). Nothing is drawn without a measurement.")
        ttk.Label(row0, text="zoom", style="Builder.CardDim.TLabel").pack(side="right", padx=(PAD["medium"], PAD["xs"]))
        self.zoom_var = tk.DoubleVar(value=1.0)
        ttk.Scale(row0, from_=0.25, to=4.0, variable=self.zoom_var, length=140, command=lambda v: self._zoom_changed()).pack(side="right")
        row1 = ttk.Frame(bar, style=LIGHT)
        row1.pack(fill="x", pady=(PAD["small"], 0))
        ttk.Label(row1, text="reference", style="Builder.CardDim.TLabel").grid(row=0, column=0, sticky="w")
        self.reference_var = tk.StringVar()
        self.reference_box = ttk.Combobox(row1, textvariable=self.reference_var, state="readonly")
        self.reference_box.grid(row=1, column=0, sticky="ew", padx=(0, PAD["medium"]))
        self.reference_box.bind("<<ComboboxSelected>>", lambda e: self._reference_chosen())
        ttk.Label(row1, text="render", style="Builder.CardDim.TLabel").grid(row=0, column=1, sticky="w")
        self.render_var = tk.StringVar()
        self.render_box = ttk.Combobox(row1, textvariable=self.render_var, state="readonly")
        self.render_box.grid(row=1, column=1, sticky="ew")
        self.render_box.bind("<<ComboboxSelected>>", lambda e: self._render_chosen())
        row1.columnconfigure(0, weight=1)
        row1.columnconfigure(1, weight=1)
        panes = ttk.Frame(self)
        panes.pack(fill="both", expand=True)
        self.left = ImagePane(panes, "Reference (owner-supplied or approved canon)")
        self.right = ImagePane(panes, "Render (actual artifact from the backend)")
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, PAD["small"]))
        self.right.grid(row=0, column=1, sticky="nsew")
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)
        self._ref_ids: list[str] = []
        self._render_ids: list[str] = []

    def bind_snapshot(self, snap: dict[str, Any]) -> None:
        self.snapshot = snap
        refs = snap.get("references", [])
        self._ref_ids = [r["id"] for r in refs]
        self.reference_box["values"] = [f"{r['id']}  [{','.join(r.get('labels') or [])}]  {r.get('canon_state')}" for r in refs]
        renders = list(snap.get("renders", []))
        self._render_ids = [r["id"] for r in renders]
        self.render_box["values"] = [f"{r['view_name']} @ {r['revision_id']}  {r['id']}  {r['evidence_label']}"
                                     + ("  STALE" if r.get("stale") else "") + ("  FAILED" if r.get("state") != "ok" else "")
                                     for r in renders]
        self.left.bind_snapshot(snap)
        self.right.bind_snapshot(snap)
        if self.mode_var.get() == "reference/render":
            if self.left.current_reference_id is None and refs:
                self.left.show_reference(refs[0]["id"])
                self.reference_box.current(0)
            if self.right.current_render_id is None and renders:
                fresh = [i for i, r in enumerate(renders) if r.get("state") == "ok" and not r.get("stale")]
                idx = fresh[-1] if fresh else len(renders) - 1
                self.right.show_render(renders[idx]["id"])
                self.render_box.current(idx)

    def _mode_changed(self) -> None:
        if self.mode_var.get() == "reference/render":
            self.left.title_var.set("Reference (owner-supplied or approved canon)")
            self.right.title_var.set("Render (actual artifact from the backend)")
            self.left.clear()
            self.right.clear()
            self.bind_snapshot(self.snapshot)

    def _reference_chosen(self) -> None:
        i = self.reference_box.current()
        if 0 <= i < len(self._ref_ids):
            self.mode_var.set("reference/render")
            self.left.title_var.set("Reference (owner-supplied or approved canon)")
            self.left.show_reference(self._ref_ids[i])

    def _render_chosen(self) -> None:
        i = self.render_box.current()
        if 0 <= i < len(self._render_ids):
            self.mode_var.set("reference/render")
            self.right.title_var.set("Render (actual artifact from the backend)")
            self.right.show_render(self._render_ids[i])

    def _zoom_changed(self) -> None:
        z = float(self.zoom_var.get())
        self.left.set_zoom(z)
        self.right.set_zoom(z)

    def _measure_changed(self) -> None:
        on = bool(self.measure_var.get())
        self.left.set_measurements(on)
        self.right.set_measurements(on)

    def show_finding(self, finding: dict[str, Any]) -> None:
        """Before and after renders of the finding, preferring its own view (R-73)."""
        renders = {r["id"]: r for r in self.snapshot.get("renders", [])}

        def pick(ids: list[str]) -> str | None:
            cands = [i for i in ids if i in renders]
            same = [i for i in cands if renders[i].get("view_name") == finding.get("view")]
            return (same or cands or [None])[0]

        before = pick(finding.get("before_render_ids") or [])
        after = pick(finding.get("after_render_ids") or [])
        self.mode_var.set("before/after")
        self.left.title_var.set(f"Before · finding {finding['id']} ({finding.get('part_id')})")
        self.right.title_var.set(f"After · finding {finding['id']} ({finding.get('part_id')})")
        if before:
            self.left.show_render(before)
        else:
            self.left.clear()
            self.left.label_var.set("no before render recorded for this finding")
        if after:
            self.right.show_render(after)
        else:
            self.right.clear()
            self.right.label_var.set("no after render yet: the finding has not been verified on a new render (R-74)")


# ============================================================================================ concept panel

class ConceptPanel(ttk.Frame):
    """Addendum R-110: open requests with copyable prompts, candidates beside the anchor, both seats' verdicts,
    approve, reject, regenerate. The approval mode is always visible (D11)."""

    def __init__(self, parent: tk.Misc, window: "BuilderWindow"):
        super().__init__(parent)
        self.window = window
        self.snapshot: dict[str, Any] = {}
        self.mode_var = tk.StringVar(value="approval mode: no project open")
        self.coverage_var = tk.StringVar(value="")
        self.attachments_var = tk.StringVar(value="")
        self.reject_reason = tk.StringVar()
        self.regenerate_note = tk.StringVar()
        self.vendor_var = tk.StringVar(value="chatgpt")
        self.model_var = tk.StringVar(value="")
        self.buttons: dict[str, ttk.Button] = {}
        self._prompts: dict[str, dict[str, Any]] = {}
        self._candidates: dict[str, dict[str, Any]] = {}
        self._selected_request: str | None = None
        self._selected_candidate: str | None = None
        self._selecting = False          # set while this code changes a tree selection itself

        head = _card(self)
        head.pack(fill="x", pady=(0, PAD["small"]))
        ttk.Label(head, textvariable=self.mode_var, style="Builder.CardAccent.TLabel", wraplength=900, justify="left").pack(anchor="w")
        _caption(head, self.coverage_var, wraplength=900).pack(anchor="w", pady=(PAD["xs"], PAD["small"]))
        actions = ttk.Frame(head, style=LIGHT)
        actions.pack(fill="x")
        self.buttons["concept_start_text"] = ttk.Button(actions, text="Start from text…", command=self._start_from_text, style="Accent.TButton")
        self.buttons["concept_start_text"].pack(side="left")
        self.buttons["concept_start_images"] = ttk.Button(actions, text="Start from seed images…", command=self._start_from_images)
        self.buttons["concept_start_images"].pack(side="left", padx=(PAD["small"], 0))
        self.buttons["study"] = ttk.Button(actions, text="Request study…", command=self._study)
        self.buttons["study"].pack(side="left", padx=(PAD["small"], 0))
        self.buttons["proceed"] = ttk.Button(actions, text="Proceed with partial set", command=self._proceed, style="Secondary.TButton")
        self.buttons["proceed"].pack(side="right")
        ToolTip(self.buttons["proceed"], "Accept the current partial reference set and hand off to intake; the missing views are recorded (R-103).")

        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True)

        # --- open requests -----------------------------------------------------------------------------------
        req = _card(body, "Open generation requests",
                    help_text="The image seat is manual (D12): copy the prompt, generate in your app with the listed attachments, "
                              "then import the files. Vendor and model are recorded as your declarations, never verified.")
        body.add(req, weight=1)
        self.requests_tree = _scrolled(req, lambda f: _tree(f, [("request", "request", 150), ("target", "target", 120), ("round", "round", 50),
                                                                 ("art_director", "art director", 80), ("expected", "images expected", 90)],
                                                             height=3))
        self.requests_tree.frame.pack(fill="x")
        self.requests_tree.bind("<<TreeviewSelect>>", lambda e: self._request_selected())
        ttk.Label(req, text="prompt (paste as-is)", style="Builder.CardDim.TLabel").pack(anchor="w", pady=(PAD["small"], 0))
        self.prompt_text = tk.Text(req, height=5, wrap="word", bg=COLORS["bg_input"], fg=COLORS["fg"], font=FONTS["body"],
                                   relief="flat", bd=0, highlightthickness=0, padx=PAD["small"], pady=PAD["xs"])
        self.prompt_text.pack(fill="x")
        self.prompt_text.configure(state="disabled")
        _caption(req, self.attachments_var, wraplength=900).pack(anchor="w", pady=(PAD["xs"], PAD["small"]))
        row = ttk.Frame(req, style=LIGHT)
        row.pack(fill="x")
        self.buttons["copy_prompt"] = ttk.Button(row, text="Copy prompt", command=self._copy_prompt, style="Accent.TButton")
        self.buttons["copy_prompt"].pack(side="left")
        ttk.Label(row, text="declared vendor", style="Builder.CardDim.TLabel").pack(side="left", padx=(PAD["medium"], PAD["xs"]))
        ttk.Combobox(row, textvariable=self.vendor_var, values=["chatgpt", "gemini", "other"], width=10, state="readonly").pack(side="left")
        ttk.Label(row, text="model as shown", style="Builder.CardDim.TLabel").pack(side="left", padx=(PAD["medium"], PAD["xs"]))
        ttk.Entry(row, textvariable=self.model_var, width=22).pack(side="left")
        self.buttons["import"] = ttk.Button(row, text="Import generated images…", command=self._import)
        self.buttons["import"].pack(side="left", padx=(PAD["small"], 0))
        self.buttons["mark_failed"] = ttk.Button(row, text="Mark failed", command=self._failed, style="Secondary.TButton")
        self.buttons["mark_failed"].pack(side="right")
        self.buttons["abandon"] = ttk.Button(row, text="Abandon", command=self._abandon, style="Secondary.TButton")
        self.buttons["abandon"].pack(side="right", padx=(0, PAD["xs"]))

        # --- candidates --------------------------------------------------------------------------------------
        cand = _card(body, "Candidates beside the anchor",
                     help_text="Candidates are hypotheses until you approve them. Both LLM seats judge each one independently "
                               "before either sees the other's verdict (R-102). Approve, reject with a reason, or regenerate.")
        body.add(cand, weight=3)
        self.candidates_tree = _scrolled(cand, lambda f: _tree(f, [("id", "candidate", 130), ("labels", "labels", 90), ("declared", "declared vendor/model", 150),
                                                                   ("verdicts", "verdicts (A, B)", 180), ("summary", "summary", 90)], height=3))
        self.candidates_tree.frame.pack(fill="x", pady=(0, PAD["small"]))
        self.candidates_tree.bind("<<TreeviewSelect>>", lambda e: self._candidate_selected())
        split = ttk.PanedWindow(cand, orient="vertical")
        split.pack(fill="both", expand=True)
        panes = ttk.Frame(split, style=LIGHT)
        split.add(panes, weight=3)
        self.anchor_pane = ImagePane(panes, "Anchor (approved canon)", size=CONCEPT_PANE_SIZE, compact=True)
        self.candidate_pane = ImagePane(panes, "Candidate (hypothesis)", size=CONCEPT_PANE_SIZE, compact=True)
        self.anchor_pane.grid(row=0, column=0, sticky="nsew", padx=(0, PAD["small"]))
        self.candidate_pane.grid(row=0, column=1, sticky="nsew")
        panes.columnconfigure(0, weight=1, uniform="pane")
        panes.columnconfigure(1, weight=1, uniform="pane")
        panes.rowconfigure(0, weight=1)
        verdicts = _card(split, "Verdicts from both seats")
        split.add(verdicts, weight=2)
        self.verdict_text = _scrolled(verdicts, lambda f: _text(f, height=7))
        self.verdict_text.frame.pack(fill="both", expand=True)
        act = ttk.Frame(cand, style=LIGHT)
        act.pack(fill="x", pady=(PAD["small"], 0))
        self.buttons["approve"] = ttk.Button(act, text="Approve", command=self._approve, style="Accent.TButton")
        self.buttons["approve"].pack(side="left")
        ttk.Label(act, text="reject reason", style="Builder.CardDim.TLabel").pack(side="left", padx=(PAD["medium"], PAD["xs"]))
        ttk.Entry(act, textvariable=self.reject_reason, width=26).pack(side="left")
        self.buttons["reject"] = ttk.Button(act, text="Reject", command=self._reject, style="Danger.TButton")
        self.buttons["reject"].pack(side="left", padx=(PAD["xs"], 0))
        ttk.Label(act, text="regenerate note", style="Builder.CardDim.TLabel").pack(side="left", padx=(PAD["medium"], PAD["xs"]))
        ttk.Entry(act, textvariable=self.regenerate_note, width=26).pack(side="left")
        self.buttons["regenerate"] = ttk.Button(act, text="Regenerate", command=self._regenerate)
        self.buttons["regenerate"].pack(side="left", padx=(PAD["xs"], 0))

    # --- rendering -------------------------------------------------------------------------------------------

    def bind_snapshot(self, snap: dict[str, Any]) -> None:
        self.snapshot = snap
        c = snap.get("concept") or {}
        seat = c.get("seat") or {}
        self.mode_var.set(f"{c.get('mode_text') or 'approval mode: ' + str(c.get('mode'))}")
        cov = c.get("coverage") or {}
        parts = [f"canon: {c.get('canon_state')}", f"image seat: {seat.get('seat')} ({seat.get('vendor')}), cost {(c.get('cost') or {}).get('kind', 'unknown')}",
                 f"art director: {(c.get('art_director') or {}).get('seat') or '-'}"]
        img = c.get("images") or {}
        parts.append(f"images {img.get('count', 0)}/{img.get('max') or 'unlimited'}")
        lines = ["  ·  ".join(parts)]
        if cov:
            lines.append("coverage: " + "; ".join(f"{v}: {e['status']} ({', '.join(e.get('approved') or e.get('candidates') or e.get('requests_open') or []) or '-'})"
                                                  for v, e in cov.items()))
        if c.get("missing"):
            lines.append("missing approved views: " + ", ".join(c["missing"]))
        if c.get("conflicts"):
            lines.append("OPEN CONFLICTS (never averaged, R-95): " + "; ".join(f"{x['id']} {x.get('region')}: {x.get('what_differs')} (by {x.get('reported_by')})" for x in c["conflicts"]))
        for esc in c.get("escalations") or []:
            if esc.get("open", True):
                lines.append(f"ESCALATED to the owner ({esc.get('view') or 'anchor'}): {esc.get('reason')}")
        if c.get("proceeded_partial"):
            pp = c["proceeded_partial"]
            lines.append(f"proceeded with a partial set by {pp.get('by')} (missing: {', '.join(pp.get('missing') or []) or 'none'})")
        self.coverage_var.set("\n".join(lines))
        # requests
        self._prompts = {p["request_id"]: p for p in c.get("prompts") or []}
        self.requests_tree.delete(*self.requests_tree.get_children())
        for p in self._prompts.values():
            self.requests_tree.insert("", "end", iid=p["request_id"], values=(p["request_id"], p["target"], p.get("round"), p.get("art_director") or "owner", p.get("expected_count")))
        if self._selected_request in self._prompts:
            self.select_request(self._selected_request)
        elif self._prompts:
            self.select_request(next(iter(self._prompts)))
        else:
            self._selected_request = None
            _set_text(self.prompt_text, "no open generation requests")
            self.attachments_var.set("")
        # candidates
        self._candidates = {x["id"]: x for x in c.get("candidates") or []}
        self.candidates_tree.delete(*self.candidates_tree.get_children())
        for x in self._candidates.values():
            d = x.get("declared") or {}
            verdicts = ", ".join(f"{s}={v.get('verdict')}" for s, v in (x.get("verdicts") or {}).items()) or "(not checked yet)"
            self.candidates_tree.insert("", "end", iid=x["id"], values=(x["id"], ",".join(x.get("labels") or []), f"{d.get('vendor') or '-'}/{d.get('model') or '-'}",
                                                                      verdicts, x.get("summary") or x.get("verdict_summary") or "-"))
        self.anchor_pane.snapshot = snap
        self.candidate_pane.snapshot = snap
        anchor = c.get("anchor")
        if anchor:
            self.anchor_pane.show_reference_entry(anchor)
        else:
            self.anchor_pane.clear()
            self.anchor_pane.label_var.set("no approved anchor yet (R-99)")
        if self._selected_candidate in self._candidates:
            self.select_candidate(self._selected_candidate)
        elif self._candidates:
            self.select_candidate(next(iter(self._candidates)))
        else:
            self._selected_candidate = None
            self.candidate_pane.clear()
            _set_text(self.verdict_text, "")

    def select_request(self, request_id: str) -> None:
        p = self._prompts.get(request_id)
        if p is None:
            return
        self._selected_request = request_id
        if self.requests_tree.exists(request_id) and tuple(self.requests_tree.selection()) != (request_id,):
            self._selecting = True
            try:
                self.requests_tree.selection_set(request_id)
            finally:
                self._selecting = False
        _set_text(self.prompt_text, p.get("prompt") or "")
        att = p.get("attachments") or []
        lines = ["attach, in this order: " + "; ".join(f"{a.get('path')} ({a.get('role')}, {a.get('reference_id')})" for a in att)] if att else ["attachments: none"]
        lines.append(f"prompt file: {p.get('prompt_file')}")
        lines.append(f"CLI equivalent: {p.get('import_command')}")
        self.attachments_var.set("\n".join(lines))

    def select_candidate(self, reference_id: str) -> None:
        x = self._candidates.get(reference_id)
        if x is None:
            return
        self._selected_candidate = reference_id
        if self.candidates_tree.exists(reference_id) and tuple(self.candidates_tree.selection()) != (reference_id,):
            self._selecting = True
            try:
                self.candidates_tree.selection_set(reference_id)
            finally:
                self._selecting = False
        self.candidate_pane.show_reference_entry(x)
        lines = [f"candidate {x['id']}  ·  summary: {x.get('summary') or x.get('verdict_summary') or '-'}",
                 reference_label(x), ""]
        anchor = (self.snapshot.get("concept") or {}).get("anchor")
        if anchor:
            lines.insert(2, "anchor: " + reference_label(anchor))
        verdicts = x.get("verdicts") or {}
        if not verdicts:
            lines.append("no verdicts yet (both LLM seats judge independently once the image is imported, R-102)")
        for seat, v in verdicts.items():
            lines.append("")
            lines.append(f"seat {seat}: {v.get('verdict')}")
            if v.get("seat_rationale"):
                lines.append(f"  assignment: {v['seat_rationale']}")
            if v.get("rationale"):
                lines.append(f"  rationale: {v['rationale']}")
            if v.get("note"):
                lines.append(f"  note: {v['note']}")
            if v.get("error"):
                lines.append(f"  error: {v['error']}")
            for inc in v.get("inconsistencies") or []:
                lines.append(f"  - {inc.get('part')} / {inc.get('region')}: {inc.get('what_differs')} [{inc.get('severity')}]")
        if x.get("approval"):
            lines.append(f"\napproval: {x['approval']}")
        if x.get("rejection"):
            lines.append(f"\nrejection: {x['rejection']}")
        _set_text(self.verdict_text, "\n".join(lines))

    def _request_selected(self) -> None:
        if self._selecting:
            return                       # raised by our own selection_set: never re-enter
        sel = self.requests_tree.selection()
        if sel and sel[0] != self._selected_request:
            self.select_request(sel[0])

    def _candidate_selected(self) -> None:
        if self._selecting:
            return
        sel = self.candidates_tree.selection()
        if sel and sel[0] != self._selected_candidate:
            self.select_candidate(sel[0])

    # --- actions ---------------------------------------------------------------------------------------------

    def _copy_prompt(self) -> None:
        p = self._prompts.get(self._selected_request or "")
        if p is None:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(p.get("prompt") or "")
        self.window.set_status(f"prompt of {p['request_id']} copied to the clipboard")

    def _import(self) -> None:
        if not self._selected_request:
            messagebox.showinfo("Import", "Select the open request the images belong to (or start the concept stage first).", parent=self.window)
            return
        files = filedialog.askopenfilenames(parent=self.window, title=f"Generated images for {self._selected_request}", filetypes=IMAGE_TYPES)
        if not files:
            return
        self.window.session.concept_import(self._selected_request, [Path(f) for f in files], vendor=self.vendor_var.get() or None,
                                           model=self.model_var.get() or None, user=self.window.user_var.get())

    def _approve(self) -> None:
        if self._selected_candidate:
            self.window.session.concept_approve([self._selected_candidate], user=self.window.user_var.get())

    def _reject(self) -> None:
        if not self._selected_candidate:
            return
        reason = self.reject_reason.get().strip()
        if not reason:
            messagebox.showinfo("Reject", "A rejection needs a reason (it is recorded, R-98).", parent=self.window)
            return
        self.window.session.concept_reject([self._selected_candidate], reason=reason, user=self.window.user_var.get())

    def _regenerate(self) -> None:
        target = None
        if self._selected_candidate:
            target = self._candidates[self._selected_candidate].get("generation_id") or self._selected_candidate
        elif self._selected_request:
            target = self._selected_request
        if target:
            self.window.session.concept_regenerate(target, note=self.regenerate_note.get(), user=self.window.user_var.get())

    def _proceed(self) -> None:
        if messagebox.askyesno("Proceed", "Accept the current partial reference set and hand off to intake? The missing views are "
                                          "recorded (R-103).", parent=self.window):
            self.window.session.concept_proceed(user=self.window.user_var.get())

    def _abandon(self) -> None:
        if self._selected_request:
            reason = _ask_string(self.window, "Abandon request", "Reason (recorded in the manifest, R-96):")
            if reason:
                self.window.session.concept_abandon(self._selected_request, reason=reason, user=self.window.user_var.get())

    def _failed(self) -> None:
        if self._selected_request:
            reason = _ask_string(self.window, "Mark failed", "What happened (recorded in the manifest, R-96):")
            if reason:
                self.window.session.concept_failed(self._selected_request, reason=reason, user=self.window.user_var.get())

    def _start_from_text(self) -> None:
        text = _ask_string(self.window, "Concept from text", "Describe the subject (the art director writes the anchor prompt; spends one LLM call):")
        if text:
            self.window.session.concept_start(from_text=text, user=self.window.user_var.get())

    def _start_from_images(self) -> None:
        files = filedialog.askopenfilenames(parent=self.window, title="Seed images (approved on entry as targets, R-99)", filetypes=IMAGE_TYPES)
        if files:
            labels = _ask_string(self.window, "Seed image labels", "Comma-separated labels for each image, separated by ';' "
                                                                    "(for example 'front;side'). Leave empty for 'other'.") or ""
            per = [[x.strip() for x in chunk.split(",") if x.strip()] or ["other"] for chunk in labels.split(";")] if labels else None
            if per is not None and len(per) != len(files):
                messagebox.showerror("Seed images", "Give one label group per image.", parent=self.window)
                return
            self.window.session.concept_start(from_images=[Path(f) for f in files], labels=per, user=self.window.user_var.get())

    def _study(self) -> None:
        part = self.window.selected_part_id()
        if not part:
            messagebox.showinfo("Study", "Select a part in the part tree first (R-104).", parent=self.window)
            return
        view = _ask_string(self.window, "Study", f"View for the study of {part} (front, side, rear, top, underside, three-quarter, detail):")
        if view:
            purpose = _ask_string(self.window, "Study", "Purpose of the study:") or f"study of {part}"
            self.window.session.concept_study(part, view=view, purpose=purpose, user=self.window.user_var.get())


def _ask_string(parent: tk.Misc, title: str, prompt: str) -> str | None:
    from tkinter import simpledialog

    return simpledialog.askstring(title, prompt, parent=parent)


# ============================================================================================ the window

class BuilderWindow(tk.Toplevel):
    def __init__(self, parent: tk.Misc | None = None, *, session: Any | None = None, config: BuilderConfig | None = None,
                 workflow_dir: str | Path | None = None, mock: str | Path | dict[str, Any] | None = None, autopoll: bool = True):
        super().__init__(parent)
        self.config_builder = config or BuilderConfig.load()
        self.session = session or BuilderSession(self.config_builder, mock=mock)
        self.snapshot: dict[str, Any] = {}
        self.buttons: dict[str, ttk.Button] = {}
        self.autopoll = autopoll
        self._busy_job: str | None = None
        self._selected_finding: str | None = None
        self._selecting = False          # set while this code changes a tree selection itself
        self._finding_ids: list[str] = []
        self._checkpoint_ids: list[str] = []
        self._component_ids: list[str] = []

        self.title("Alloy · Model Builder")
        self.geometry(f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}")
        self.minsize(1100, 700)
        self.configure(bg=COLORS["bg"])
        apply_dark_theme(self)
        _install_styles(self)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.workflow_var = tk.StringVar(value=str(workflow_dir or ""))
        self.status_var = tk.StringVar(value="idle")
        self.user_var = tk.StringVar(value=_default_user())
        self.stage_var = tk.StringVar(value="execution: idle")
        self.components_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="approval mode: -")
        self.attended_var = tk.BooleanVar(value=self.config_builder.attended)
        self.assign_var = tk.StringVar(value=" ".join(f"{k}={v}" for k, v in (self.config_builder.assignments or {}).items()))
        self.waive_rationale = tk.StringVar()
        self.reopen_reason = tk.StringVar()
        self.correction_note = tk.StringVar()
        self.feedback_var = tk.StringVar()
        self.checkpoint_var = tk.StringVar()
        self.component_var = tk.StringVar()
        self.region_name = tk.StringVar(value="target")
        self.region_purpose = tk.StringVar(value="target_region")
        self.limit_vars: dict[str, tk.StringVar] = {name: tk.StringVar(value=str(self.config_builder.limits.get(name, ""))) for name in DEFAULT_LIMITS}
        self.limits_note_var = tk.StringVar(value="")
        self._limits_shown: dict[str, str] = {}

        self._build()
        if workflow_dir:
            self.open_workflow(workflow_dir)
        if autopoll:
            self.after(POLL_MS, self._poll)

    # --- layout ----------------------------------------------------------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=PAD["medium"], pady=(PAD["medium"], PAD["small"]))
        ttk.Label(top, text="Model Builder", style="Heading.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.mode_var, style="Accent.TLabel").pack(side="left", padx=(PAD["large"], 0))
        ttk.Entry(top, textvariable=self.user_var, width=12).pack(side="right")
        ttk.Label(top, text="user", style="Dim.TLabel").pack(side="right", padx=(PAD["medium"], PAD["xs"]))
        self.buttons["open"] = ttk.Button(top, text="Open", command=lambda: self.open_workflow(self.workflow_var.get()), style="Accent.TButton")
        self.buttons["open"].pack(side="right")
        ttk.Button(top, text="Browse…", command=self._browse_workflow).pack(side="right", padx=(PAD["xs"], PAD["xs"]))
        ttk.Entry(top, textvariable=self.workflow_var, width=52).pack(side="right")
        ttk.Label(top, text="workflow", style="Dim.TLabel").pack(side="right", padx=(0, PAD["xs"]))

        bottom = ttk.Frame(self, style="Card.TFrame", padding=(PAD["medium"], PAD["xs"]))
        bottom.pack(side="bottom", fill="x", padx=PAD["medium"], pady=(PAD["small"], PAD["medium"]))
        ttk.Label(bottom, textvariable=self.status_var, style="Builder.Status.TLabel", wraplength=760, justify="left").pack(side="left")
        ttk.Label(bottom, textvariable=self.stage_var, style="Builder.CardDim.TLabel", wraplength=640, justify="right").pack(side="right")

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=PAD["medium"])
        self.main_pane = main
        self.after(150, self._place_sashes)

        left = ttk.Notebook(main, style="Builder.TNotebook")
        main.add(left, weight=1)
        left.add(self._build_project_tab(left), text="  Project  ")
        left.add(self._build_agents_tab(left), text="  Agents  ")
        left.add(self._build_run_tab(left), text="  Run  ")

        center = ttk.Frame(main)
        main.add(center, weight=3)
        self.compare = ComparisonPanel(center)
        self.compare.pack(fill="both", expand=True, padx=PAD["small"])

        right = ttk.Notebook(main, style="Builder.TNotebook")
        main.add(right, weight=2)
        right.add(self._build_parts_tab(right), text="  Parts  ")
        right.add(self._build_findings_tab(right), text="  Findings  ")
        right.add(self._build_coverage_tab(right), text="  Coverage  ")
        self.concept_panel = ConceptPanel(right, self)
        right.add(self.concept_panel, text="  Concept  ")
        right.add(self._build_activity_tab(right), text="  Activity  ")
        self.right_notebook = right
        self.left_notebook = left
        right.bind("<<NotebookTabChanged>>", lambda e: self._place_sashes())

    def _place_sashes(self) -> None:
        """Initial column split: the comparison panel gets the middle; Treeview request widths would otherwise
        squeeze it. The user can drag the sashes afterwards."""
        try:
            w = self.main_pane.winfo_width()
            if w <= 1:
                self.after(150, self._place_sashes)
                return
            concept = self.right_notebook.select() == str(self.concept_panel)
            first, second = self._sash_fractions(concept=concept)
            self.main_pane.sashpos(0, int(w * first))
            self.main_pane.sashpos(1, int(w * second))
        except (tk.TclError, AttributeError):
            pass

    @staticmethod
    def _sash_fractions(*, concept: bool) -> tuple[float, float]:
        return SASH_FRACTIONS["concept" if concept else "default"]

    def _tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=(0, PAD["small"], 0, 0))
        return tab

    def _build_project_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = self._tab(parent)
        card = _card(tab, "Project")
        card.pack(fill="x", pady=(0, PAD["small"]))
        self.project_text = _text(card, height=8)
        self.project_text.pack(fill="x")
        refs = _card(tab, "References", help_text="Originals are copied unmodified and hashed; regions live in original pixels (R-27). "
                                                   "Owner-supplied images are canon; generated images are hypotheses until approved (R-94).")
        refs.pack(fill="both", expand=True)
        self.references_tree = _scrolled(refs, lambda f: _tree(f, [("id", "reference", 120), ("labels", "labels", 80), ("kind", "kind", 70),
                                                                   ("canon", "canon", 80), ("evidence", "evidence of original", 100), ("size", "size", 70)], height=8))
        self.references_tree.frame.pack(fill="both", expand=True)
        self.references_tree.bind("<<TreeviewSelect>>", lambda e: self._reference_selected())
        row = ttk.Frame(refs, style=LIGHT)
        row.pack(fill="x", pady=(PAD["small"], 0))
        self.new_ref_labels = tk.StringVar(value="front")
        _labelled_entry(row, "labels", self.new_ref_labels, width=14).pack(side="left", padx=(0, PAD["small"]))
        kind = ttk.Frame(row, style=LIGHT)
        kind.pack(side="left", padx=(0, PAD["small"]))
        ttk.Label(kind, text="kind", style="Builder.CardDim.TLabel").pack(anchor="w")
        self.new_ref_kind = tk.StringVar(value="target")
        ttk.Combobox(kind, textvariable=self.new_ref_kind, values=["target", "previous_attempt", "rejected", "hypothesis"], width=14, state="readonly").pack()
        self.new_ref_composite = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="composite sheet", variable=self.new_ref_composite, style="Builder.Card.TCheckbutton").pack(side="left", pady=(PAD["medium"], 0))
        self.buttons["add_reference"] = ttk.Button(row, text="Add image…", command=self._add_reference)
        self.buttons["add_reference"].pack(side="right", pady=(PAD["medium"], 0))
        src = ttk.Frame(refs, style=LIGHT)
        src.pack(fill="x", pady=(PAD["small"], 0))
        ttk.Label(src, text="revision 0 (never registered by creating or opening the project, R-93)", style="Builder.CardDim.TLabel").pack(side="left")
        self.buttons["source_empty"] = ttk.Button(src, text="Register empty scene", command=self._register_empty_source)
        self.buttons["source_empty"].pack(side="right")
        ToolTip(self.buttons["source_empty"], "Build an empty scene (one tagged collection) through Blender and register it as "
                                              "revision 0. For a project that starts from references or a concept set only.")
        self.buttons["source_register"] = ttk.Button(src, text="Register source .blend…", command=self._register_source)
        self.buttons["source_register"].pack(side="right", padx=(0, PAD["xs"]))
        ToolTip(self.buttons["source_register"], "Validate an existing .blend in separate Blender processes (identities, reopen) and copy "
                                                 "it unmodified into revisions/ as immutable revision 0. The file you pick is only read.")
        reg = ttk.Frame(refs, style=LIGHT)
        reg.pack(fill="x", pady=(PAD["small"], 0))
        _labelled_entry(reg, "region name", self.region_name, width=16).pack(side="left", padx=(0, PAD["small"]))
        purpose = ttk.Frame(reg, style=LIGHT)
        purpose.pack(side="left", padx=(0, PAD["small"]))
        ttk.Label(purpose, text="purpose", style="Builder.CardDim.TLabel").pack(anchor="w")
        ttk.Combobox(purpose, textvariable=self.region_purpose, values=["target_region", "detail_crop"], width=13, state="readonly").pack()
        self.buttons["draw_region"] = ttk.Button(reg, text="Draw region on reference", command=self._arm_region)
        self.buttons["draw_region"].pack(side="right", pady=(PAD["medium"], 0))
        ToolTip(self.buttons["draw_region"], "Select the reference, then drag a rectangle on its preview. The region is stored in the "
                                             "original image's pixels, whatever the zoom (R-26, R-27). A composite sheet needs a target region.")
        return tab

    def _build_agents_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = self._tab(parent)
        card = _card(tab, "Agents", help_text="Three tiers per capability: declared by configuration, checked locally from --help and --version, "
                                             "and live-verified by real probes (R-15). The effective reasoning setting comes from the provider's own "
                                             "output (R-20); nothing is substituted silently.")
        card.pack(fill="both", expand=True)
        self.agents_tree = _scrolled(card, lambda f: _tree(f, [("label", "seat", 40), ("provider", "provider", 60), ("model", "model", 110),
                                                               ("reasoning", "reasoning requested → effective", 200), ("preflight", "preflight", 90),
                                                               ("cli", "CLI", 80)], height=3))
        self.agents_tree.frame.pack(fill="x", pady=(0, PAD["small"]))
        self.agents_tree.bind("<<TreeviewSelect>>", lambda e: self._agent_selected())
        self.agent_text = _scrolled(card, lambda f: _text(f, height=16))
        self.agent_text.frame.pack(fill="both", expand=True)
        row = ttk.Frame(card, style=LIGHT)
        row.pack(fill="x", pady=(PAD["small"], 0))
        self.buttons["preflight"] = ttk.Button(row, text="Preflight (local, free)", command=lambda: self.session.preflight(live=False))
        self.buttons["preflight"].pack(side="left")
        self.buttons["preflight_live"] = ttk.Button(row, text="Preflight (live)…", command=self._preflight_live, style="Accent.TButton")
        self.buttons["preflight_live"].pack(side="left", padx=(PAD["small"], 0))
        ToolTip(self.buttons["preflight_live"], "Real probes per agent; spends provider usage. Asks for confirmation first.")
        return tab

    def _build_run_tab(self, parent: tk.Misc) -> ttk.Frame:
        outer_tab = self._tab(parent)
        scroll, tab = _scroll_frame(outer_tab)
        scroll.pack(fill="both", expand=True)
        self.run_tab_canvas = scroll.canvas  # type: ignore[attr-defined]
        stage = _card(tab, "Stage, task, ownership")
        stage.pack(fill="x", pady=(0, PAD["small"]))
        self.stage_text = _text(stage, height=8)
        self.stage_text.pack(fill="x")
        ttk.Label(stage, textvariable=self.components_var, style="Builder.CardAccent.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(PAD["small"], 0))

        ctl = _card(tab, "Controls", help_text=ATTENDED_HELP)
        ctl.pack(fill="x", pady=(0, PAD["small"]))
        r1 = ttk.Frame(ctl, style=LIGHT)
        r1.pack(fill="x")
        self.buttons["start"] = ttk.Button(r1, text="Start", command=self._start, style="Accent.TButton", width=8)
        self.buttons["start"].pack(side="left")
        self.buttons["resume"] = ttk.Button(r1, text="Resume", command=lambda: self.session.resume_run(user=self.user_var.get()), width=8)
        self.buttons["resume"].pack(side="left", padx=(PAD["xs"], 0))
        self.buttons["pause"] = ttk.Button(r1, text="Pause", command=self.session.pause, width=8)
        self.buttons["pause"].pack(side="left", padx=(PAD["xs"], 0))
        self.buttons["cancel"] = ttk.Button(r1, text="Cancel", command=self._cancel, style="Danger.TButton", width=8)
        self.buttons["cancel"].pack(side="left", padx=(PAD["xs"], 0))
        r1b = ttk.Frame(ctl, style=LIGHT)
        r1b.pack(fill="x", pady=(PAD["xs"], 0))
        attended = ttk.Checkbutton(r1b, text="attended: stop for my acceptance before advancing past a component",
                                   variable=self.attended_var, style="Builder.Card.TCheckbutton")
        attended.pack(side="left")
        ToolTip(attended, ATTENDED_HELP)
        r1c = ttk.Frame(ctl, style=LIGHT)
        r1c.pack(fill="x", pady=(PAD["xs"], 0))
        assign = _labelled_entry(r1c, "assignments for this run (role=SEAT ...; overrides builder.assignments)", self.assign_var, width=40)
        assign.pack(side="left", fill="x", expand=True)
        ToolTip(assign, "Pre-assign roles to seats for the run, for example build=B plan=A. Roles: brief, plan, build, corrector, "
                        "reviewer, verifier, reassessor. Recorded on the run with its rationale (R-8). A seat never reviews, verifies, "
                        "or reassesses its own operation, even under an override: that stops the run with the R-107 reason.")

        ttk.Separator(ctl, orient="horizontal").pack(fill="x", pady=PAD["small"])
        r2 = ttk.Frame(ctl, style=LIGHT)
        r2.pack(fill="x")
        comp = ttk.Frame(r2, style=LIGHT)
        comp.pack(side="left", fill="x", expand=True)
        ttk.Label(comp, text="component", style="Builder.CardDim.TLabel").pack(anchor="w")
        self.component_box = ttk.Combobox(comp, textvariable=self.component_var, state="readonly")
        self.component_box.pack(fill="x")
        self.buttons["accept"] = ttk.Button(r2, text="Accept", command=self._accept, style="Accent.TButton")
        self.buttons["accept"].pack(side="left", padx=(PAD["small"], 0), pady=(PAD["medium"], 0))
        ToolTip(self.buttons["accept"], "Accept the component at its current revision; binds the evidence and dependency versions (R-6).")
        r2b = ttk.Frame(ctl, style=LIGHT)
        r2b.pack(fill="x", pady=(PAD["xs"], 0))
        _labelled_entry(r2b, "reopen reason", self.reopen_reason, width=22).pack(side="left", fill="x", expand=True)
        self.buttons["reopen"] = ttk.Button(r2b, text="Reopen", command=self._reopen)
        self.buttons["reopen"].pack(side="left", padx=(PAD["small"], 0), pady=(PAD["medium"], 0))

        ttk.Separator(ctl, orient="horizontal").pack(fill="x", pady=PAD["small"])
        r3 = ttk.Frame(ctl, style=LIGHT)
        r3.pack(fill="x")
        _labelled_entry(r3, "correction note (selected finding)", self.correction_note, width=22).pack(side="left", fill="x", expand=True)
        self.buttons["correct"] = ttk.Button(r3, text="Request correction", command=self._request_correction)
        self.buttons["correct"].pack(side="left", padx=(PAD["small"], 0), pady=(PAD["medium"], 0))
        r3b = ttk.Frame(ctl, style=LIGHT)
        r3b.pack(fill="x", pady=(PAD["xs"], 0))
        _labelled_entry(r3b, "waiver rationale (selected finding)", self.waive_rationale, width=22).pack(side="left", fill="x", expand=True)
        self.buttons["waive"] = ttk.Button(r3b, text="Waive", command=self._waive, style="Secondary.TButton")
        self.buttons["waive"].pack(side="left", padx=(PAD["small"], 0), pady=(PAD["medium"], 0))

        ttk.Separator(ctl, orient="horizontal").pack(fill="x", pady=PAD["small"])
        r4 = ttk.Frame(ctl, style=LIGHT)
        r4.pack(fill="x")
        ck = ttk.Frame(r4, style=LIGHT)
        ck.pack(side="left", fill="x", expand=True)
        ttk.Label(ck, text="checkpoint", style="Builder.CardDim.TLabel").pack(anchor="w")
        self.checkpoint_box = ttk.Combobox(ck, textvariable=self.checkpoint_var, state="readonly")
        self.checkpoint_box.pack(fill="x")
        self.buttons["restore"] = ttk.Button(r4, text="Restore", command=self._restore, style="Secondary.TButton")
        self.buttons["restore"].pack(side="left", padx=(PAD["small"], 0), pady=(PAD["medium"], 0))
        ToolTip(self.buttons["restore"], "Creates a new revision whose parent is the checkpoint's revision; history is never rewritten (R-46).")
        r5 = ttk.Frame(ctl, style=LIGHT)
        r5.pack(fill="x", pady=(PAD["xs"], 0))
        _labelled_entry(r5, "feedback (applied at the next safe boundary)", self.feedback_var, width=22).pack(side="left", fill="x", expand=True)
        self.buttons["feedback"] = ttk.Button(r5, text="Send", command=self._feedback)
        self.buttons["feedback"].pack(side="left", padx=(PAD["small"], 0), pady=(PAD["medium"], 0))

        lim = _card(tab, "Limits (zero means unlimited)", help_text="Changes apply at the next safe boundary of a running engine, or at once "
                                                                    "when the run is stopped; every change is journaled (R-85, R-89).")
        lim.pack(fill="x", pady=(0, PAD["small"]))
        grid = ttk.Frame(lim, style=LIGHT)
        grid.pack(fill="x")
        for i, name in enumerate(DEFAULT_LIMITS):
            ttk.Label(grid, text=name, style="Builder.CardDim.TLabel").grid(row=i // 3 * 2, column=i % 3, sticky="w", padx=(0, PAD["small"]))
            ttk.Entry(grid, textvariable=self.limit_vars[name], width=10).grid(row=i // 3 * 2 + 1, column=i % 3, sticky="w", padx=(0, PAD["small"]), pady=(0, PAD["xs"]))
        lrow = ttk.Frame(lim, style=LIGHT)
        lrow.pack(fill="x", pady=(PAD["xs"], 0))
        self.buttons["apply_limits"] = ttk.Button(lrow, text="Apply limits", command=self._apply_limits)
        self.buttons["apply_limits"].pack(side="right")               # packed first so a long note never squeezes it
        _caption(lrow, self.limits_note_var, wraplength=300).pack(side="left", fill="x", expand=True)

        cons = _card(tab, "Consumption and limits", help_text="Enforceable limits are labelled separately from estimates; an unknown cost is "
                                                              "never treated as zero and never as proof that a cap is enforced (R-85).")
        cons.pack(fill="both", expand=True)
        self.consumption_text = _scrolled(cons, lambda f: _text(f, height=10))
        self.consumption_text.frame.pack(fill="both", expand=True)
        return outer_tab

    def _build_parts_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = self._tab(parent)
        card = _card(tab, "Part tree", help_text="Parent hierarchy as the tree; construction relations (covers, sits under, attached to, ...) "
                                                "as child rows, kept apart from scheduling dependencies (R-35, R-36).")
        card.pack(fill="both", expand=True)
        self.parts_tree = _scrolled(card, lambda f: _tree(f, [("state", "state", 70), ("evidence", "evidence", 70), ("confidence", "confidence", 70),
                                                              ("owner", "owner", 50), ("component", "component", 80)], show="tree headings", height=14))
        self.parts_tree.column("#0", width=200, anchor="w")
        self.parts_tree.heading("#0", text="part / relation", anchor="w")
        self.parts_tree.frame.pack(fill="both", expand=True)
        self.parts_tree.bind("<<TreeviewSelect>>", lambda e: self._part_selected())
        self.part_text = _text(card, height=6)
        self.part_text.pack(fill="x", pady=(PAD["small"], 0))
        return tab

    def _build_findings_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = self._tab(parent)
        card = _card(tab, "Findings", help_text="Severity and confidence are separate fields; a finding closes only when the expected improvement "
                                               "is verified on a new render, never because a script ran (R-73, R-74).")
        card.pack(fill="both", expand=True)
        self.findings_tree = _scrolled(card, lambda f: _tree(f, [("id", "finding", 110), ("state", "state", 80), ("part", "part", 80), ("view", "view", 60),
                                                                 ("severity", "severity", 60), ("confidence", "confidence", 70), ("mismatch", "observed mismatch", 220)], height=8))
        self.findings_tree.frame.pack(fill="both", expand=True)
        self.findings_tree.bind("<<TreeviewSelect>>", lambda e: self._finding_selected())
        self.finding_text = _scrolled(card, lambda f: _text(f, height=12))
        self.finding_text.frame.pack(fill="both", expand=True, pady=(PAD["small"], 0))
        return tab

    def _build_coverage_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = self._tab(parent)
        card = _card(tab, "Review coverage", help_text="By part, view, and revision. Sampling is recorded and never presented as inspection of every instance (R-72).")
        card.pack(fill="both", expand=True)
        self.coverage_tree = _scrolled(card, lambda f: _tree(f, [("part", "part", 90), ("view", "view", 90), ("revision", "revision", 120),
                                                                 ("by", "inspected by", 70), ("instances", "instances", 80), ("sampling", "sampling", 100)], height=20))
        self.coverage_tree.frame.pack(fill="both", expand=True)
        return tab

    def _build_activity_tab(self, parent: tk.Misc) -> ttk.Frame:
        tab = self._tab(parent)
        card = _card(tab, "Activity")
        card.pack(fill="both", expand=True)
        self.activity = _scrolled(card, lambda f: _text(f, height=30))
        self.activity.frame.pack(fill="both", expand=True)
        return tab

    # --- queue -----------------------------------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                msg = self.session.events.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        if self.autopoll and self.winfo_exists():
            self.after(POLL_MS, self._poll)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "busy":
            self._busy_job = msg[1]
            self.status_var.set(f"working: {msg[1]}  (the window stays responsive; provider and Blender work run on the worker thread)")
            self._set_busy(True)
        elif kind == "done":
            self._busy_job = None
            self._log(f"[done] {msg[1]}: {_short(msg[2])}")
            self.status_var.set(f"done: {msg[1]}")
            self._set_busy(False)
        elif kind == "error":
            self._busy_job = None
            self._log(f"[ERROR] {msg[1]}: {msg[2]}")
            self.status_var.set(f"error in {msg[1]}: {msg[2]}")
            self._set_busy(False)
        elif kind == "event":
            self._log(_format_event(msg[1]))
        elif kind == "snapshot":
            self.apply_snapshot(msg[1])
        elif kind == "closed":
            self.status_var.set("session closed")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for name in ("start", "resume", "accept", "reopen", "waive", "correct", "restore", "preflight", "preflight_live", "open", "add_reference"):
            self.buttons[name].configure(state=state)
        for name in ("approve", "reject", "regenerate", "import", "proceed", "concept_start_text", "concept_start_images", "study", "abandon", "mark_failed"):
            self.concept_panel.buttons[name].configure(state=state)

    def _log(self, line: str) -> None:
        self.activity.configure(state="normal")
        self.activity.insert("end", line + "\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    # --- snapshot --------------------------------------------------------------------------------------------

    def apply_snapshot(self, snap: dict[str, Any]) -> None:
        self.snapshot = snap
        p = snap.get("project") or {}
        st = snap.get("stage") or {}
        self.workflow_var.set(p.get("workflow_dir") or self.workflow_var.get())
        self.title(f"Alloy · Model Builder · {p.get('name') or ''}")
        # project
        lines = [f"{p.get('name')}  ·  asset: {p.get('asset_name')}  ·  {p.get('id')}", f"workflow: {p.get('workflow_dir')}",
                 f"first component: {p.get('first_component') or '-'}  ·  preset: {p.get('preset_id') or '-'}  ·  fixture: {p.get('fixture')}"]
        if p.get("target_reference"):
            lines.append(f"preset target reference (never read until intake): {p['target_reference']}")
        if p.get("existing_source"):
            lines.append(f"existing source (never opened): {p['existing_source']}")
        if p.get("status_note"):
            lines.append(p["status_note"])
        b = snap.get("blender") or {}
        lines.append(f"Blender: {'available' if b.get('available') else 'NOT FOUND'}  ·  {b.get('version')}  ·  {b.get('executable') or ''}")
        _set_text(self.project_text, "\n".join(lines))
        self.references_tree.delete(*self.references_tree.get_children())
        for r in snap.get("references", []):
            self.references_tree.insert("", "end", iid=r["id"], values=(r["id"], ",".join(r.get("labels") or []), r.get("kind"), r.get("canon_state"),
                                                                      "yes" if r.get("evidence_of_original") else "no (hypothesis)", f"{r.get('width')}x{r.get('height')}"),
                                        open=True)
            for g in r.get("regions") or []:
                self.references_tree.insert(r["id"], "end", iid=g["id"], values=(g["id"], g.get("name"), g.get("purpose"), "region", "original pixels", str(g.get("bbox"))))
        limits = (snap.get("consumption") or {}).get("limits") or {}
        if limits:
            self._limits_shown = {}
            notes = []
            for name, lim in limits.items():
                if name in self.limit_vars:
                    value = str(lim.get("value"))
                    self.limit_vars[name].set(value)
                    self._limits_shown[name] = value
                if name == "max_cost_usd":
                    notes.append(f"max_cost_usd enforceable={lim.get('enforceable')}: {lim.get('note')}")
            self.limits_note_var.set("; ".join(notes) or "limits in force")
        # agents
        self.agents_tree.delete(*self.agents_tree.get_children())
        for a in snap.get("agents", []):
            pf = a.get("preflight") or {}
            reasoning = f"{a.get('reasoning_requested') or 'not exposed'} → {a.get('reasoning_effective') or '?'} ({a.get('reasoning_note')})"
            pf_text = ("ok" if pf.get("ok") else ("blocked" if pf.get("ok") is False else "not run")) + (" live" if pf.get("live") else " local") if pf.get("ok") is not None else "not run"
            self.agents_tree.insert("", "end", iid=a["label"], values=(a["label"], a.get("provider"), a.get("model_effective") or a.get("model"), reasoning, pf_text,
                                                                     f"{pf.get('cli_version') or '?'}"))
        self._agent_selected()
        # stage
        self.stage_var.set(f"execution {st.get('execution')}  ·  stop reason {st.get('stop_reason') or '-'}  ·  stage {st.get('stage') or '-'}  ·  "
                           f"{'attended' if st.get('attended') else 'unattended'}")
        task = st.get("task") or {}
        own = st.get("ownership")
        lines = [f"run {st.get('run_id') or '-'}  ·  {st.get('execution')}  ·  stop reason {st.get('stop_reason') or '-'}  ·  stage {st.get('stage') or '-'}",
                 f"{'attended' if st.get('attended') else 'unattended'}  ·  handoffs: {st.get('handoffs')}"]
        if task:
            lines.append(f"task {task.get('id')}  {task.get('kind')} [{task.get('state')}]  owner {task.get('owner')}  base {task.get('base_revision_id')}")
            lines.append(f"  rationale: {task.get('rationale')}")
            lines.append(f"  expected: {task.get('expected_outcome')}  ·  parts: {', '.join(task.get('part_ids') or [])}")
        for o in st.get("assignment_overrides") or []:
            lines.append(f"assignment override {o['role']}={o['seat']} ({o['source']}): {o['rationale']}")
        lines.append("ownership: " + (f"{own['holder']} holds {own['resource']} at {own['base_revision_id']} since {own['granted_at']}" if own else "free (no holder)"))
        for op in st.get("active_operations") or []:
            lines.append(f"active operation {op['id']} {op['kind']} [{op['state']}] by {op['agent']}: {op.get('intent')}")
        if st.get("last_note"):
            lines.append(f"last note: {st['last_note'].get('stop_reason')}: {st['last_note'].get('note')}")
        for q in snap.get("questions_for_user") or []:
            lines.append(f"QUESTION for you: {q}")
        _set_text(self.stage_text, "\n".join(lines))
        comps = snap.get("components") or []
        self._component_ids = [c["id"] for c in comps]
        self.components_var.set("\n".join(f"{c['name']} ({c['id']}): review {c['review_state']}  ·  acceptance {c['acceptance_state']}  ·  "
                                          f"open findings {c['open_findings']}  ·  revision {c.get('revision_id')}" for c in comps) or "no component yet")
        self.component_box["values"] = [f"{c['id']} {c['name']}" for c in comps]
        if comps and self.component_box.current() < 0:
            self.component_box.current(0)
        cks = snap.get("checkpoints") or []
        self._checkpoint_ids = [c["id"] for c in cks]
        self.checkpoint_box["values"] = [f"{c['id']} {c.get('name')} @ {c.get('revision_id')}" for c in cks]
        if cks and self.checkpoint_box.current() < 0:
            self.checkpoint_box.current(len(cks) - 1)
        self.mode_var.set((snap.get("concept") or {}).get("mode_text") or "approval mode: -")
        _set_text(self.consumption_text, _consumption_text(snap))
        # parts
        self._fill_parts(snap)
        # findings
        self.findings_tree.delete(*self.findings_tree.get_children())
        self._finding_ids = []
        for f in snap.get("findings", []):
            self._finding_ids.append(f["id"])
            self.findings_tree.insert("", "end", iid=f["id"], values=(f["id"], f["state"], f.get("part_id"), f.get("view"), f.get("severity"),
                                                                    f.get("confidence"), f.get("observed_mismatch")))
        if self._selected_finding in self._finding_ids:
            self._show_finding_detail(self._selected_finding)
        # coverage
        self.coverage_tree.delete(*self.coverage_tree.get_children())
        for c in snap.get("coverage", []):
            self.coverage_tree.insert("", "end", values=(c.get("part_id"), c.get("view_name"), c.get("revision_id"), c.get("inspected_by"),
                                                         c.get("instances_inspected"), c.get("sampling_strategy") or "-"))
        # comparison and concept
        self.compare.bind_snapshot(snap)
        self.concept_panel.bind_snapshot(snap)
        if not self._busy_job:
            self.status_var.set(f"execution {st.get('execution')}" + (f", {st.get('stop_reason')}" if st.get("stop_reason") else ""))

    def _fill_parts(self, snap: dict[str, Any]) -> None:
        tree = self.parts_tree
        tree.delete(*tree.get_children())
        parts = snap.get("parts", [])
        ids = {p["id"] for p in parts}
        placed: set[str] = set()

        def insert(p: dict[str, Any], parent: str) -> None:
            tree.insert(parent, "end", iid=p["id"], text=p["id"] if p.get("name") in (None, p["id"]) else f"{p['id']}  ({p['name']})",
                        values=(p.get("state"), p.get("evidence_status") or "?", p.get("confidence") or "-", p.get("owner") or "-", p.get("component") or "-"))
            placed.add(p["id"])
            for r in p.get("relations") or []:
                text = f"{r['type']} → {r['to_part']}" if r.get("direction") == "out" else f"← {r['type']} {r['from_part']}"
                tree.insert(p["id"], "end", iid=f"{p['id']}::{r['id']}", text=text, values=("relation", r.get("evidence") or "", "", "", ""))

        for p in parts:
            if not p.get("parent_id") or p["parent_id"] not in ids:
                insert(p, "")
        remaining = [p for p in parts if p["id"] not in placed]
        guard = 0
        while remaining and guard < 50:
            guard += 1
            for p in list(remaining):
                if p["parent_id"] in placed:
                    insert(p, p["parent_id"])
                    remaining.remove(p)
        for p in remaining:
            insert(p, "")

    # --- selection handlers ----------------------------------------------------------------------------------

    def selected_part_id(self) -> str | None:
        sel = self.parts_tree.selection()
        if not sel:
            return None
        return sel[0].split("::", 1)[0]

    def _part_selected(self) -> None:
        if self._selecting:
            return                       # selection made by select_finding: keep its before/after view
        pid = self.selected_part_id()
        p = next((x for x in self.snapshot.get("parts", []) if x["id"] == pid), None)
        if p is None:
            return
        lines = [f"{p['id']}  {p.get('name')}  ·  state {p.get('state')}  ·  blender ids {p.get('blender_ids')}",
                 f"evidence: {p.get('evidence_status') or 'unknown'}  ·  confidence: {p.get('confidence') or '-'}  ·  owner: {p.get('owner') or '-'}  ·  component: {p.get('component') or '-'}",
                 f"interpretation: {p.get('interpretation') or '-'}"]
        for d in p.get("dimensions") or []:
            lines.append(f"  dimension {d}")
        for q in p.get("questions") or []:
            lines.append(f"  question: {q}")
        _set_text(self.part_text, "\n".join(lines))
        closeups = [r for r in self.snapshot.get("renders", []) if r.get("part_id") == pid and r.get("state") == "ok"]
        if closeups and self.compare.mode_var.get() == "reference/render":
            self.compare.right.show_render(closeups[-1]["id"])

    def _reference_selected(self) -> None:
        sel = self.references_tree.selection()
        if sel:
            iid = sel[0]
            if self.references_tree.parent(iid):
                iid = self.references_tree.parent(iid)          # a region row selects its reference
            self.compare.mode_var.set("reference/render")
            self.compare.left.show_reference(iid)

    def _arm_region(self) -> None:
        pane = self.compare.left
        if pane.current_reference_id is None or self.compare.mode_var.get() != "reference/render":
            self.set_status("select a reference in the comparison panel first, then draw the region on it")
            return
        name = self.region_name.get().strip()
        if not name:
            self.set_status("give the region a name first")
            return
        ref_id = pane.current_reference_id
        purpose = self.region_purpose.get()

        def done(bbox: list[int]) -> None:
            self.session.add_region(ref_id, name, bbox, purpose=purpose, user=self.user_var.get())
            self.set_status(f"region {name!r} {bbox} (original pixels) submitted for {ref_id}")

        pane.on_region = done
        pane.region_mode = True
        self.set_status(f"drag a rectangle on reference {ref_id} to record {purpose} {name!r}")

    def _apply_limits(self) -> None:
        changes = {name: var.get().strip() for name, var in self.limit_vars.items()
                   if var.get().strip() != self._limits_shown.get(name, var.get().strip() if not self._limits_shown else None)}
        if not self._limits_shown:
            changes = {name: var.get().strip() for name, var in self.limit_vars.items() if var.get().strip()}
        if not changes:
            self.set_status("no limit changed")
            return
        self.session.set_limits(changes, user=self.user_var.get())
        self.set_status(f"limit change submitted: {changes}")

    def _agent_selected(self) -> None:
        sel = self.agents_tree.selection()
        label = sel[0] if sel else (self.agents_tree.get_children()[0] if self.agents_tree.get_children() else None)
        a = next((x for x in self.snapshot.get("agents", []) if x["label"] == label), None)
        seat = self.snapshot.get("image_seat") or {}
        lines = []
        if a:
            pf = a.get("preflight") or {}
            lines += [f"seat {a['label']}: {a.get('provider')}  ·  model {a.get('model')} (effective {a.get('model_effective') or '?'})  ·  executable: {a.get('executable') or 'PATH'}",
                      f"reasoning requested {a.get('reasoning_requested') or 'not exposed'} → effective {a.get('reasoning_effective') or '?'}: {a.get('reasoning_note')}",
                      f"preflight ok={pf.get('ok')} live={pf.get('live')} at {pf.get('at')}  ·  CLI {pf.get('cli_version')} {pf.get('cli_path') or ''}",
                      f"invocations: {a.get('invocations')}  ·  probe cost: {pf.get('probe_cost')}"]
            for fb in a.get("reasoning_fallbacks") or []:
                lines.append(f"REASONING DOWNGRADE (reported, not silent): {fb}")
            for blocker in pf.get("blockers") or []:
                lines.append(f"BLOCKER: {blocker}")
            lines.append("")
            for name, t in (pf.get("capabilities") or {}).items():
                lines.append(f"  {name}: declared={t.get('declared')}  local={t.get('local')}  live={t.get('live')}" + (f"  ({t.get('evidence')})" if t.get("evidence") else ""))
        d = seat.get("declared") or {}
        spf = seat.get("preflight") or {}
        lines.append("")
        lines.append(f"image seat I: {d.get('seat')}  ·  vendor {d.get('vendor')}  ·  model {d.get('model') or '-'}  ·  declared={spf.get('declared')} "
                     f"local={spf.get('local')} live={spf.get('live')}")
        _set_text(self.agent_text, "\n".join(lines))

    def _finding_selected(self) -> None:
        if self._selecting:
            return                       # raised by our own selection_set: never re-enter
        sel = self.findings_tree.selection()
        if sel and sel[0] != self._selected_finding:
            self.select_finding(sel[0])

    def select_finding(self, finding_id: str) -> None:
        if finding_id not in self._finding_ids:
            return
        self._selected_finding = finding_id
        self._selecting = True
        try:
            if tuple(self.findings_tree.selection()) != (finding_id,):
                self.findings_tree.selection_set(finding_id)
            self._show_finding_detail(finding_id)
            f = next(x for x in self.snapshot["findings"] if x["id"] == finding_id)
            self.compare.show_finding(f)
            if f.get("part_id") and self.parts_tree.exists(f["part_id"]):
                if tuple(self.parts_tree.selection()) != (f["part_id"],):
                    self.parts_tree.selection_set(f["part_id"])
                self.parts_tree.see(f["part_id"])
        finally:
            self._selecting = False

    def _show_finding_detail(self, finding_id: str) -> None:
        f = next((x for x in self.snapshot.get("findings", []) if x["id"] == finding_id), None)
        if f is None:
            return
        lines = [f"{f['id']}  [{f['state']}]  ·  part {f.get('part_id')}  ·  region {f.get('region') or '-'}  ·  view {f.get('view') or '-'}",
                 f"severity {f.get('severity')}  ·  confidence {f.get('confidence')}  ·  kind {f.get('kind')}  ·  reported by {f.get('reported_by')}",
                 f"observed: {f.get('observed_mismatch')}", f"proposed correction: {f.get('proposed_correction') or '-'}",
                 f"expected improvement: {f.get('expected_improvement') or '-'}",
                 f"evidence renders: {', '.join(f.get('evidence_render_ids') or f.get('evidence_refs') or []) or '-'}",
                 f"before renders: {', '.join(f.get('before_render_ids') or []) or '-'}", f"after renders: {', '.join(f.get('after_render_ids') or []) or '-'}",
                 f"source revision: {f.get('source_revision_id')}"]
        for h in f.get("alternative_hypotheses") or []:
            lines.append(f"alternative hypothesis: {h}")
        for a in f.get("attempts") or []:
            lines.append(f"attempt {a.get('attempt_no')} {a['id']}: {a['state']}  before {a.get('before_render_ids')} after {a.get('after_render_ids')}")
        if f.get("resolution"):
            lines.append(f"resolution: {f['resolution']}")
        if f.get("last_verdict"):
            lines.append(f"last verdict: {f['last_verdict']}")
        _set_text(self.finding_text, "\n".join(lines))

    # --- actions ---------------------------------------------------------------------------------------------

    def open_workflow(self, workflow_dir: str | Path) -> None:
        self.workflow_var.set(str(workflow_dir))
        self.session.open(Path(workflow_dir))

    def _browse_workflow(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Workflow project directory (contains project.json)")
        if d:
            self.open_workflow(d)

    def _add_reference(self) -> None:
        files = filedialog.askopenfilenames(parent=self, title="Reference images", filetypes=IMAGE_TYPES)
        labels = [x.strip() for x in self.new_ref_labels.get().split(",") if x.strip()] or ["other"]
        for f in files:
            self.session.add_reference(Path(f), labels=labels, kind=self.new_ref_kind.get(), composite=bool(self.new_ref_composite.get()), user=self.user_var.get())

    def _preflight_live(self) -> None:
        if messagebox.askyesno("Live preflight", LIVE_PREFLIGHT_WARNING, parent=self):
            self.session.preflight(live=True)

    def _start(self) -> None:
        assignments = parse_assignment_text(self.assign_var.get())
        if assignments is None:
            messagebox.showinfo("Assignments", "Assignments are role=SEAT pairs separated by spaces, for example build=B plan=A.", parent=self)
            return
        if assignments:
            self.session.start_run(attended=bool(self.attended_var.get()), component_name=None, assignments=assignments)
        else:
            self.session.start_run(attended=bool(self.attended_var.get()), component_name=None)

    def _register_source(self) -> None:
        f = filedialog.askopenfilename(parent=self, title="Source .blend to register as revision 0 (read only, copied unmodified)",
                                       filetypes=[("Blender files", "*.blend"), ("All files", "*.*")])
        if f:
            self.session.register_source(Path(f), user=self.user_var.get())

    def _register_empty_source(self) -> None:
        self.session.register_empty_source(user=self.user_var.get())

    def _cancel(self) -> None:
        if messagebox.askyesno("Cancel run", "Cancel the run? In-flight provider and Blender processes are killed and the run is "
                                             "cancelled after reconciliation.", parent=self):
            self.session.cancel(user=self.user_var.get())

    def _selected_component(self) -> str | None:
        i = self.component_box.current()
        return self._component_ids[i] if 0 <= i < len(self._component_ids) else None

    def _accept(self) -> None:
        comp = self._selected_component()
        if comp:
            self.session.accept_component(comp, user=self.user_var.get())

    def _reopen(self) -> None:
        comp = self._selected_component()
        reason = self.reopen_reason.get().strip()
        if comp and reason:
            self.session.reopen_component(comp, reason=reason, user=self.user_var.get())
        elif comp:
            messagebox.showinfo("Reopen", "Reopening needs a reason (recorded with the superseded acceptance, R-6).", parent=self)

    def _waive(self) -> None:
        rationale = self.waive_rationale.get().strip()
        if self._selected_finding and rationale:
            self.session.waive_finding(self._selected_finding, rationale=rationale, user=self.user_var.get())
        elif self._selected_finding:
            messagebox.showinfo("Waive", "A waiver needs a rationale (R-75).", parent=self)

    def _request_correction(self) -> None:
        if self._selected_finding:
            self.session.request_correction(self._selected_finding, note=self.correction_note.get(), user=self.user_var.get())

    def _restore(self) -> None:
        i = self.checkpoint_box.current()
        if 0 <= i < len(self._checkpoint_ids):
            if messagebox.askyesno("Restore checkpoint", "Restore creates a new revision whose parent is the checkpoint's revision; history is "
                                                         "never rewritten and reviews are invalidated (R-46, R-39). Continue?", parent=self):
                self.session.restore_checkpoint(self._checkpoint_ids[i], user=self.user_var.get())

    def _feedback(self) -> None:
        text = self.feedback_var.get().strip()
        if text:
            self.session.feedback(text, user=self.user_var.get())
            self.feedback_var.set("")

    def _on_close(self) -> None:
        try:
            self.session.close(timeout=2.0)
        finally:
            self.destroy()


# ============================================================================================ helpers

def _default_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "user"


def _short(value: Any, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def parse_assignment_text(text: str) -> dict[str, str] | None:
    """``build=B plan=A`` (space or comma separated) to ``{"build": "B", "plan": "A"}``; ``None`` when malformed.
    Roles and seats are validated by the engine (R-8, R-107)."""
    out: dict[str, str] = {}
    for item in text.replace(",", " ").split():
        if "=" not in item:
            return None
        role, seat = item.split("=", 1)
        if not role.strip() or not seat.strip():
            return None
        out[role.strip()] = seat.strip()
    return out


def _format_event(ev: dict[str, Any]) -> str:
    kind = ev.get("event")
    if kind == "stage":
        return f"[stage] {ev.get('stage')}"
    if kind == "invocation":
        return f"[agent {ev.get('label')}] {ev.get('purpose')}: {ev.get('outcome')} ({ev.get('invocation_id')})"
    if kind == "operation":
        return f"[operation] {ev.get('op_id')} by {ev.get('agent')}: {ev.get('state')}"
    if kind == "run.stopped":
        return f"[stop] {ev.get('stop_reason')}: {ev.get('note')}"
    if kind == "preflight":
        report = ev.get("report") or {}
        return "[preflight] " + ", ".join(f"{k}={'ok' if v.get('ok') else 'blocked'}" for k, v in report.items() if isinstance(v, dict) and "ok" in v)
    return f"[{kind}] " + ", ".join(f"{k}={_short(v, 80)}" for k, v in ev.items() if k not in ("event", "at"))


def _quantity(q: dict[str, Any] | None) -> str:
    if not q:
        return "unknown"
    k = q.get("kind")
    if k in ("measured", "estimated"):
        return f"{q.get('value')} ({k}, {q.get('source')})"
    if k == "not_applicable":
        return "not applicable"
    return "unknown"


def _consumption_text(snap: dict[str, Any]) -> str:
    c = snap.get("consumption") or {}
    if not c:
        return "no run yet"
    lines = [f"elapsed {c.get('elapsed_s')} s  ·  requests {c.get('requests', {}).get('completed')} completed, {c.get('requests', {}).get('inflight')} in flight  ·  "
             f"renders {c.get('renders', {}).get('completed')} completed, {c.get('renders', {}).get('inflight')} in flight",
             f"cost measured: {_quantity(c.get('cost', {}).get('measured'))}  ·  estimated: {_quantity(c.get('cost', {}).get('estimated'))}  ·  "
             f"invocations with unknown cost: {c.get('cost', {}).get('unknown_invocations')} (unknown is never zero)",
             "", "limits"]
    for name, lim in (c.get("limits") or {}).items():
        lines.append(f"  {name} = {lim.get('value')}{' (unlimited)' if lim.get('unlimited') else ''}  ·  enforceable={lim.get('enforceable')}"
                     + (f"  ·  {lim.get('note')}" if lim.get("note") else ""))
    lines.append("")
    if c.get("external"):
        lines.append(f"external (preflight probes): {c['external']}")
    if c.get("per_agent"):
        lines.append(f"per agent: {c['per_agent']}")
    if c.get("findings_at_attempt_limit"):
        lines.append(f"findings at the attempt limit: {c['findings_at_attempt_limit']}")
    lines.append(f"contributions (committed operations): {snap.get('contributions')}")
    lines.append(f"transport retries: {c.get('transport_retries')}  ·  correction attempts: {c.get('correction_attempts')}")
    return "\n".join(lines)


def open_builder_window(parent: tk.Misc, config_path: Path | None = None, workflow_dir: str | Path | None = None) -> BuilderWindow:
    """Entry point for the menu hook in ``gui/app.py``."""
    cfg = BuilderConfig.load(config_path) if config_path else BuilderConfig.load()
    return BuilderWindow(parent, config=cfg, workflow_dir=workflow_dir)


def run_standalone(workflow_dir: str | Path | None = None, config_path: str | Path | None = None,
                   mock: str | Path | None = None) -> None:
    """``python -m gui.builder_view [workflow_dir] [screenplay.json]``: the window without the chat app."""
    root = tk.Tk()
    root.withdraw()
    cfg = BuilderConfig.load(config_path) if config_path else BuilderConfig.load()
    win = BuilderWindow(root, config=cfg, workflow_dir=workflow_dir, mock=mock)
    win.protocol("WM_DELETE_WINDOW", lambda: (win._on_close(), root.destroy()))
    root.wait_window(win)
    try:
        root.destroy()
    except tk.TclError:
        pass


if __name__ == "__main__":  # pragma: no cover
    import sys

    run_standalone(sys.argv[1] if len(sys.argv) > 1 else None, mock=sys.argv[2] if len(sys.argv) > 2 else None)
