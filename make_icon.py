"""Generate ai-chat.ico: the Alloy trefoil (BRANDING.md, approved 2026-08-16).

One continuous strand drawn parametrically as a depth-sorted thick polyline
(branding/trefoil_v2.py holds the geometry — the same code that rendered the
approved comparison sheet). Every .ico size regenerates deterministically
from that one function; sizes below 32px use the heavier small-size weights
so the strand and its molten crossings survive the taskbar.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "branding"))
from trefoil_v2 import draw_trefoil, small_mark

SIZES = [16, 24, 32, 48, 64, 128, 256]


def icon_frame(size):
    return small_mark(size) if size < 32 else draw_trefoil(size)


frames = [icon_frame(s) for s in SIZES]
frames[-1].save(r"C:\ai-chat\ai-chat.ico", format="ICO",
                sizes=[(s, s) for s in SIZES],
                append_images=frames[:-1])
# alloy.ico is a byte-identical copy the desktop/taskbar shortcuts point at:
# Windows' icon cache keys on PATH, and the old chat-bubbles icon was cached
# so hard under ai-chat.ico that no refresh dislodged it — a fresh filename
# was the fix. Keep both written so regeneration can't drift them apart.
import shutil
shutil.copyfile(r"C:\ai-chat\ai-chat.ico", r"C:\ai-chat\alloy.ico")
print("icon written (ai-chat.ico + alloy.ico)")
