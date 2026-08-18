"""Alloy trefoil v2 — deterministic drawing code (BRANDING.md, locked spec).

One continuous closed strand that only *appears* to be three loops: the most
literal drawing of "alloy". Rendered as a parametric curve stroked as a
depth-sorted thick polyline with correct over-under crossings, flat colors:

  body  #302A49  graphite-violet, the cold metal
  seam  #F4B942 -> #FFF1C2  amber-to-ivory, molten at the crossings

The seam is ONE uninterrupted gradient swept once around the strand — a
single continuous color function of the curve parameter t (never per-lobe
bands): intensity |sin 3t|^p peaks exactly at the six crossing contacts, and
inside a peak the color runs amber -> ivory (hottest at the contact).

This geometry is what make_icon.py will encode once the mark is approved.
Run:  python branding/trefoil_v2.py   -> branding/trefoil-v2-comparison.png
"""

import math
import os

import numpy as np
from PIL import Image, ImageDraw

BODY = (0x30, 0x2A, 0x49)
AMBER = (0xF4, 0xB9, 0x42)
IVORY = (0xFF, 0xF1, 0xC2)

N = 1200                      # curve samples per revolution
A = 2.4                       # 2nd-harmonic weight: bigger = rounder, more
                              # open knot (2.0 is the cramped classic)
SEAM_SIGMA = 0.07             # gaussian half-width of each molten bump (rad)
SEAM_SPLIT = 0.50             # body->amber below, amber->ivory above


def trefoil_xyz(t):
    """A trefoil: (x, y) is the drawing, z orders the crossings."""
    return (math.sin(t) + A * math.sin(2 * t),
            math.cos(t) - A * math.cos(2 * t),
            -math.sin(3 * t))


def _solve_crossings():
    """Self-intersection parameters, numerically (deterministic; runs once).
    Returns (t_under, t_over) — the strand passes under at t_under + 2k*pi/3
    and over at t_over + 2k*pi/3, by the curve's 3-fold symmetry."""
    n = 6000
    ts = np.linspace(0, 2 * math.pi, n, endpoint=False)
    x = np.sin(ts) + A * np.sin(2 * ts)
    y = np.cos(ts) - A * np.cos(2 * ts)
    z = -np.sin(3 * ts)
    best = None
    for i in range(n // 3):           # one fundamental domain is enough
        d2 = (x - x[i]) ** 2 + (y - y[i]) ** 2
        dt = np.abs(ts - ts[i])
        dt = np.minimum(dt, 2 * math.pi - dt)
        d2[dt < 0.5] = np.inf         # ignore the curve's own neighbourhood
        j = int(np.argmin(d2))
        if best is None or d2[j] < best[0]:
            best = (d2[j], i, j)
    _, i, j = best
    t_a, t_b = float(ts[i]), float(ts[j])
    return (t_a, t_b) if z[i] < z[j] else (t_b, t_a)


T_UNDER, T_OVER = _solve_crossings()


def _crossing_ts():
    return [base + 2 * math.pi * k / 3
            for base in (T_UNDER, T_OVER) for k in range(3)]


def _wrap(d):
    return (d + math.pi) % (2 * math.pi) - math.pi


def strand_color(t, sigma=None):
    """One continuous sweep around the whole strand: graphite body warming to
    an amber->ivory seam at the six crossing contacts. A single function of
    t — never per-lobe color bands."""
    sigma = sigma or SEAM_SIGMA
    s = max(math.exp(-_wrap(t - c) ** 2 / (2 * sigma ** 2))
            for c in _crossing_ts())
    if s < SEAM_SPLIT:
        f = s / SEAM_SPLIT
        a, b = BODY, AMBER
    else:
        f = (s - SEAM_SPLIT) / (1 - SEAM_SPLIT)
        a, b = AMBER, IVORY
    return tuple(round(a[i] + (b[i] - a[i]) * f) for i in range(3))


def _fit(points, size, margin_frac):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    scale = (size * (1 - 2 * margin_frac)) / span
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2
    return lambda x, y: ((x - cx) * scale + size / 2,
                         (y - cy) * scale + size / 2)


def _stroke(draw, pts, colors, width):
    """Thick polyline with round joints, one color per segment. Colors are
    RGB tuples on RGBA layers, plain ints on "L" mask layers."""
    r = width / 2
    ink = lambda c: c + (255,) if isinstance(c, tuple) else c
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        c = ink(colors[i])
        draw.line([x0, y0, x1, y1], fill=c, width=round(width))
        draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=c)
    x0, y0 = pts[0]
    draw.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=ink(colors[0]))


def draw_trefoil(size, width_frac=0.13, gap_frac=0.035, margin_frac=0.06,
                 supersample=4, seam_sigma=None):
    """Render the mark on a transparent square. Depth-sorted crossings: the
    full strand is drawn closed, then each over-arc erases a band beneath
    itself (the visual gap) and re-draws on top."""
    S = size * supersample
    ts = [2 * math.pi * i / N for i in range(N + 1)]
    xyz = [trefoil_xyz(t) for t in ts]
    to_px = _fit(xyz, S, margin_frac)
    pts = [to_px(x, y) for x, y, _ in xyz]
    colors = [strand_color((ts[i] + ts[i + 1]) / 2, seam_sigma)
              for i in range(N)]
    W = S * width_frac
    GAP = S * gap_frac

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _stroke(ImageDraw.Draw(img), pts, colors, W)

    # Over-arcs centered on the real crossings (t = T_OVER + 2k*pi/3, where
    # z > 0). Erase slightly SHORTER than the redraw so the erase ring never
    # nicks the strand where the arc rejoins it — the gap survives only where
    # the under strand passes. delta is a tight window on purpose: the next
    # crossing is only 0.54 rad away along the strand, and an arc that long
    # redraws the WRONG strand on top there and breaks the weave. It just
    # needs to bridge its own crossing: ~(W/2 + GAP) of curve each side.
    # eps (how far the redraw outruns the erase) must exceed the erase cap
    # RADIUS in parameter terms, or the cap punches a notch in the strand
    # just past the redraw's end — the "dark disc stamped on the glow" bug.
    delta, eps = 0.42, 0.16
    for k in range(3):
        t_over = T_OVER + 2 * math.pi * k / 3
        # contiguous index run centered on the crossing (modular, so an arc
        # spanning t = 2*pi never becomes two disjoint polylines)
        ic = round(t_over / (2 * math.pi) * N)
        di = round(delta / (2 * math.pi) * N)
        de = round((delta - eps) / (2 * math.pi) * N)
        arc_pts = [pts[i % N] for i in range(ic - di, ic + di + 1)]
        arc_cols = [colors[i % N] for i in range(ic - di, ic + di)]
        erase_pts = [pts[i % N] for i in range(ic - de, ic + de + 1)]
        mask = Image.new("L", (S, S), 0)
        _stroke(ImageDraw.Draw(mask), erase_pts,
                [255] * (len(erase_pts) - 1), W + 2 * GAP)
        arr = np.array(img)
        arr[np.array(mask) > 0] = 0
        img = Image.fromarray(arr)
        _stroke(ImageDraw.Draw(img), arc_pts, arc_cols, W)

    return img.resize((size, size), Image.LANCZOS)


def small_mark(size=16):
    """Small-size variant: heavier stroke, wider gap, wider seam — the weight
    boost every icon pipeline applies below ~32px so both the strand and the
    molten crossings survive the taskbar."""
    return draw_trefoil(size, width_frac=0.19, gap_frac=0.05,
                        margin_frac=0.02, seam_sigma=0.16)


def render_comparison(out_path):
    """Proof sheet: the full-size mark plus LITERAL 16px marks on near-black
    and near-white, with 8x nearest-neighbour insets so the pixels are
    inspectable. The 16px tiles are pasted 1:1 — no scaling."""
    NEAR_BLACK = (0x14, 0x12, 0x18)
    NEAR_WHITE = (0xF4, 0xF2, 0xEE)
    sheet = Image.new("RGB", (720, 400), (0x20, 0x1D, 0x26))
    d = ImageDraw.Draw(sheet)

    big = draw_trefoil(300)
    sheet.paste(big, (36, 50), big)
    d.text((36, 366), "trefoil v2 - 300px", fill=(0x8D, 0x87, 0x98))

    tiny = small_mark(16)
    for col, bg, label in ((0, NEAR_BLACK, "16px on near-black"),
                           (1, NEAR_WHITE, "16px on near-white")):
        x0 = 396 + col * 164
        tile = Image.new("RGB", (148, 148), bg)
        # literal 16px, centered
        tile.paste(tiny, ((148 - 16) // 2, 18), tiny)
        # 8x nearest-neighbour inset purely for inspection
        zoom = tiny.resize((128, 128), Image.NEAREST)
        zt = Image.new("RGB", (128, 128), bg)
        zt.paste(zoom, (0, 0), zoom)
        tile.paste(zt, (10, 44))
        sheet.paste(tile, (x0, 50))
        d.text((x0, 204), label, fill=(0x8D, 0x87, 0x98))
        d.text((x0, 218), "(top: literal / below: 8x zoom)",
               fill=(0x5E, 0x59, 0x68))

    # a second literal-size strip: 16, 24, 32 on both grounds
    d.text((396, 250), "literal 16 / 24 / 32:", fill=(0x8D, 0x87, 0x98))
    for col, bg in ((0, NEAR_BLACK), (1, NEAR_WHITE)):
        x0 = 396 + col * 164
        strip = Image.new("RGB", (148, 56), bg)
        x = 12
        for s in (16, 24, 32):
            m = small_mark(s) if s < 32 else draw_trefoil(s)
            strip.paste(m, (x, (56 - s) // 2), m)
            x += s + 18
        sheet.paste(strip, (x0, 270))

    sheet.save(out_path)
    return out_path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "trefoil-v2-comparison.png")
    print("wrote", render_comparison(out))
