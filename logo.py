"""Lookhere logo: the overlay's record dot and mic meter on a dark rounded tile.
The dot takes the state colour (idle / recording / building). Drawn at 512px and scaled down."""
import os

from PIL import Image, ImageDraw, ImageFilter

THEMES = {
    "dark": dict(TILE=(10, 10, 10, 255), EDGE=(255, 255, 255, 38), IDLE=(250, 250, 250), REC=(255, 59, 48),
                 BUSY=(212, 255, 58), BARS=(250, 250, 250, 240)),
    # lime dot gets a black ring on the white tile (RING) so it stays visible
    "light": dict(TILE=(250, 250, 250, 255), EDGE=(0, 0, 0, 40), IDLE=(12, 12, 12), REC=(232, 36, 26),
                  BUSY=(212, 255, 58), BARS=(12, 12, 12, 240)),
}


def set_theme(name):
    globals().update(THEMES[name])


set_theme("dark")


def draw_logo(dot=REC, size=64):
    S = 512
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle((16, 16, S - 16, S - 16), radius=132, fill=TILE)
    # faint top sheen, like light on the overlay's glass
    sheen = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sheen).rounded_rectangle((16, 16, S - 16, S // 2), radius=132, fill=22 if TILE[0] < 128 else 0)
    tile.alpha_composite(Image.merge("RGBA", (*[Image.new("L", (S, S), 255)] * 3, sheen)))
    img.alpha_composite(tile)
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((16, 16, S - 16, S - 16), radius=132, outline=EDGE, width=8)
    img.alpha_composite(edge)

    fx = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(fx)
    small = size <= 24
    # Tray sizes get a simpler glyph: bigger dot, two chunky bars, no glow (fine detail smudges).
    cx, cy, r = (178, 256, 112) if small else (190, 256, 92)
    if not small and sum(dot) / 3 > 90:  # a glow suits a bright dot; a dark one just smudges
        glow = Image.new("L", (S, S), 0)
        ImageDraw.Draw(glow).ellipse((cx - r - 26, cy - r - 26, cx + r + 26, cy + r + 26), fill=120)
        glow = glow.filter(ImageFilter.GaussianBlur(22))
        img.alpha_composite(Image.merge("RGBA", (*[Image.new("L", (S, S), c) for c in dot], glow)))
    if TILE[0] > 128 and sum(dot) / 3 > 150:  # light tile, light dot: ring it in black
        d.ellipse((cx - r - 26, cy - r - 26, cx + r + 26, cy + r + 26), fill=BARS[:3] + (255,))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dot + (255,))
    # mic meter: rounded bars, tall in the middle
    bw, bars = (58, ((350, 170), (434, 270))) if small else (34, ((328, 120), (380, 210), (432, 120)))
    for x, h in bars:
        d.rounded_rectangle((x - bw / 2, cy - h / 2, x + bw / 2, cy + h / 2), radius=bw / 2,
                            fill=BARS)
    img.alpha_composite(fx)
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path, dot=REC):
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    draw_logo(dot, 256).save(path, sizes=[(s, s) for s in sizes])


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    save_ico(os.path.join(here, "lookhere.ico"))
    print("lookhere.ico written")
