"""Generate ai-chat.ico: two overlapping chat bubbles (Claude orange, GPT green)."""
from PIL import Image, ImageDraw

def bubble(draw, x, y, w, h, r, color, tail="left"):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=color)
    ty = y + h - 2
    if tail == "left":
        draw.polygon([(x + r, ty), (x + r + w * 0.18, ty), (x + r * 0.4, ty + h * 0.28)], fill=color)
    else:
        draw.polygon([(x + w - r, ty), (x + w - r - w * 0.18, ty), (x + w - r * 0.4, ty + h * 0.28)], fill=color)

def dots(draw, cx, cy, spacing, rad, color):
    for i in (-1, 0, 1):
        draw.ellipse([cx + i * spacing - rad, cy - rad, cx + i * spacing + rad, cy + rad], fill=color)

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

ORANGE = (217, 119, 87, 255)   # Claude
GREEN = (16, 163, 127, 255)    # GPT
WHITE = (255, 255, 255, 230)

# back bubble (GPT, green, upper right)
bubble(d, 96, 28, 138, 92, 26, GREEN, tail="right")
dots(d, 96 + 69, 28 + 46, 26, 8, WHITE)
# front bubble (Claude, orange, lower left)
bubble(d, 22, 96, 150, 100, 28, ORANGE, tail="left")
dots(d, 22 + 75, 96 + 50, 28, 9, WHITE)

img.save(r"C:\ai-chat\ai-chat.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written")
